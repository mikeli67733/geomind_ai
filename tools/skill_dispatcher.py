# -*- coding: utf-8 -*-
"""
LLM skill dispatcher — bridges the Copilot backend to local tools.

When the backend LLM issues a tool_call, this module routes it to the
appropriate local function (raster op, vector op, AI task, or QGIS
processing algorithm).
"""
import os
import json
import requests
from datetime import datetime, timedelta
from typing import Optional

from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsMapLayer,
    QgsApplication,
    QgsProcessingParameterDefinition,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
)
from qgis.PyQt.QtCore import QVariant
from qgis.utils import iface

from ..core.constants import (
    TIANDITU_API_KEY,
    TIANDITU_GEOCODER_URL,
    find_class_ids_by_keywords,
    get_model_key_by_mode,
)
from ..core.logger import get_logger
from .raster_ops import (
    calc_spectral_index,
    run_pca,
    dem_analysis,
    spatial_filter,
    area_statistics,
    kmeans_cluster,
    raster_diff,
    image_enhance,
    raster_polygonize,
)
from .vector_ops import vector_simplify_and_smooth
import tempfile
from osgeo import gdal

logger = get_logger("tools.skill_dispatcher")

STAC_API_URL = "https://earth-search.aws.element84.com/v1/search"


# ===========================================================================
# Layer helpers
# ===========================================================================

def get_layer_by_name(layer_name: str, layer_type: str = "raster"):
    """Look up a QGIS layer by name with optional type validation."""
    layers = QgsProject.instance().mapLayersByName(layer_name)
    if not layers:
        raise ValueError(f"找不到图层: '{layer_name}'")
    layer = layers[0]
    if layer_type == "raster" and not isinstance(layer, QgsRasterLayer):
        raise ValueError(f"图层 '{layer_name}' 必须是栅格图层")
    if layer_type == "vector" and not isinstance(layer, QgsVectorLayer):
        raise ValueError(f"图层 '{layer_name}' 必须是矢量图层")
    return layer


def get_active_layers() -> str:
    """Return a summary string of all layers in the current project."""
    layers = QgsProject.instance().mapLayers().values()
    if not layers:
        return "当前 QGIS 工程中无图层。"
    info = []
    for l in layers:
        l_type = "栅格" if isinstance(l, QgsRasterLayer) else "矢量"
        info.append(f"{l.name()} ({l_type})")
    return f"当前活动图层有: {', '.join(info)}"


# ===========================================================================
# 1. Geocoding & Sentinel-2 STAC fetcher
# ===========================================================================

def skill_geocode_address(
    address_text: str,
    lon: Optional[float] = None,
    lat: Optional[float] = None,
) -> str:
    """Geocode an address and zoom the QGIS canvas to the location."""
    source_type = "天地图"

    try:
        if iface is None:
            return "错误：获取不到QGIS iface对象，无法操作地图画布"

        if lon is not None and lat is not None:
            lon = float(lon)
            lat = float(lat)
            source_type = "大模型地理常识估算/国外定位"
        else:
            if not TIANDITU_API_KEY or len(TIANDITU_API_KEY) < 10:
                return "地图tk密钥无效，请通过环境变量 GEOMIND_TIANDITU_TK 配置"

            ds_data = json.dumps({"keyWord": address_text}, ensure_ascii=False)
            params = {"ds": ds_data, "tk": TIANDITU_API_KEY}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.tianditu.gov.cn/",
            }

            import time
            time.sleep(0.3)

            from ..api.http_client import HttpClient
            client = HttpClient(request_timeout=15, retries=1)
            try:
                resp = client.get(
                    TIANDITU_GEOCODER_URL,
                    params=params,
                    headers=headers,
                    timeout=15,
                    retry_on_status=True,
                )
                js = resp.json()
            finally:
                client.close()

            if js.get("status") != "0":
                return f"地图解析失败: {js.get('msg', '未知错误')}。若是国外地址，请直接传入 lon 和 lat 经纬度调用。"

            location = js.get("location")
            if not location:
                return (
                    f"地图未匹配到国内结果：'{address_text}'（可能为国外地点或生僻地名）。\n"
                    f"请大语言模型根据自身知识库评估该地点的 WGS84 经度(lon)和纬度(lat)，重新调用此函数。"
                )

            lon = float(location["lon"])
            lat = float(location["lat"])

        layer_name = f"定位_{address_text[:10]}"
        vlayer = QgsVectorLayer("Point?crs=EPSG:4326", layer_name, "memory")
        prov = vlayer.dataProvider()
        prov.addAttributes([
            QgsField("address", QVariant.String),
            QgsField("lon", QVariant.Double),
            QgsField("lat", QVariant.Double),
            QgsField("source", QVariant.String),
        ])
        vlayer.updateFields()

        feat = QgsFeature()
        feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        feat.setAttributes([address_text, lon, lat, source_type])
        prov.addFeature(feat)
        vlayer.updateExtents()
        QgsProject.instance().addMapLayer(vlayer)

        canvas = iface.mapCanvas()
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        dest_crs = canvas.mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
        canvas_point = transform.transform(QgsPointXY(lon, lat))

        canvas.setCenter(canvas_point)
        canvas.zoomScale(50000)
        canvas.refresh()

        return f"地址定位完成：{address_text} (经度={lon:.6f}, 纬度={lat:.6f})，画布已跳转。"

    except Exception as e:
        logger.error("Geocode failed: %s", e)
        return f"地址解析/定位失败: {e}"

def search_and_load_sentinel2(
    extent_bbox: list,
    date_start: str = None,
    date_end: str = None,
    max_cloud_cover: int = 15,
    auto_load_first: bool = True,
    band_type: str = "4band"
) -> str:
    """
    底层 AWS STAC 检索 + 精准按当前屏幕范围裁剪的虚拟 VRT 流式加载
    """
    today = datetime.now()
    if not date_end:
        date_end = today.strftime("%Y-%m-%d")
    if not date_start:
        date_start = (today - timedelta(days=14)).strftime("%Y-%m-%d")

    payload = {
        "collections": ["sentinel-2-l2a"],
        "bbox": extent_bbox,
        "datetime": f"{date_start}T00:00:00Z/{date_end}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": max_cloud_cover}},
        "limit": 5,
        "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}]
    }

    try:
        resp = requests.post(STAC_API_URL, json=payload, timeout=15)
        if resp.status_code != 200:
            return f"STAC 检索服务异常 (HTTP {resp.status_code})"

        features = resp.json().get("features", [])
        if not features:
            return f"在 {date_start} 至 {date_end} 期间未检索到云量 < {max_cloud_cover}% 的影像，请放宽日期或云量限制。"

        result_lines = [f"🛰️ 成功检索到 {len(features)} 景符合条件的 Sentinel-2 影像："]
        loaded_layer_name = ""

        for i, item in enumerate(features):
            props = item.get("properties", {})
            acq_time = props.get("datetime", "")[:10]
            cloud = props.get("eo:cloud_cover", 0.0)
            item_id = item.get("id", "")
            assets = item.get("assets", {})

            result_lines.append(f"{i+1}. 拍摄日期: `{acq_time}` | 云量: `{cloud:.1f}%` | 景号: `{item_id}`")

            if i == 0 and auto_load_first:
                layer = None
                
                # 裁剪参数：严格限定只截取当前屏幕范围 [min_lon, min_lat, max_lon, max_lat]
                warp_options = gdal.WarpOptions(
                    format="VRT",
                    outputBounds=[extent_bbox[0], extent_bbox[1], extent_bbox[2], extent_bbox[3]],
                    outputBoundsSRS="EPSG:4326",
                    resampleAlg="bilinear"
                )

                # 方案 A：合成并裁剪 4波段/6波段 多光谱（RGB+近红外）
                if band_type in ("4band", "full") and all(k in assets for k in ("red", "green", "blue", "nir")):
                    band_urls = [
                        f"/vsicurl/{assets['red']['href']}",    # Band 1: 红光 (B04)
                        f"/vsicurl/{assets['green']['href']}",  # Band 2: 绿光 (B03)
                        f"/vsicurl/{assets['blue']['href']}",   # Band 3: 蓝光 (B02)
                        f"/vsicurl/{assets['nir']['href']}",    # Band 4: 近红外 (B08)
                    ]
                    if band_type == "full" and "swir16" in assets and "swir22" in assets:
                        band_urls.append(f"/vsicurl/{assets['swir16']['href']}")
                        band_urls.append(f"/vsicurl/{assets['swir22']['href']}")
                        band_desc = "6波段多光谱"
                    else:
                        band_desc = "4波段多光谱(含近红外)"

                    # 1. 先合成虚拟多波段
                    raw_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_raw.vrt")
                    gdal.BuildVRT(raw_vrt, band_urls, options=gdal.BuildVRTOptions(separate=True))

                    # 2. 虚拟裁剪至屏幕视口范围
                    clipped_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_screen.vrt")
                    gdal.Warp(clipped_vrt, raw_vrt, options=warp_options)

                    layer_name = f"Sentinel2_{acq_time}_{band_desc}(屏幕范围)_云量{cloud:.1f}%"
                    layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")

                # 方案 B：降级使用 3波段真彩色 (TCI)
                if layer is None or not layer.isValid():
                    visual_asset = assets.get("visual") or assets.get("overview")
                    if visual_asset:
                        clipped_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_rgb_screen.vrt")
                        gdal.Warp(clipped_vrt, f"/vsicurl/{visual_asset['href']}", options=warp_options)

                        layer_name = f"Sentinel2_{acq_time}_真彩色(屏幕范围)_云量{cloud:.1f}%"
                        layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")

                if layer and layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    loaded_layer_name = layer_name
                    if iface and iface.mapCanvas():
                        iface.mapCanvas().refresh()

        if loaded_layer_name:
            result_lines.append(f"\n🎉 **已自动为您加载【当前屏幕范围】的影像**：`{loaded_layer_name}`\n"
                                f"📐 *图层范围已自动裁切为当前地图视口大小，无多余边缘。*")

        return "\n".join(result_lines)
    except Exception as e:
        return f"检索 Sentinel-2 影像时发生异常: {e}"


def _geocode_place_bbox(place_name: str) -> list:
    """内部辅助：将地名解析为 WGS84 经纬度范围 [min_lon, min_lat, max_lon, max_lat]"""
    # 常用主要城市/区域快速坐标库（秒级响应免网络请求）
    quick_bboxes = {
        "北京": [115.7, 39.4, 117.4, 41.0],
        "上海": [120.8, 30.7, 122.2, 31.9],
        "广州": [112.9, 22.5, 114.1, 23.9],
        "深圳": [113.7, 22.4, 114.6, 22.9],
        "成都": [103.7, 30.3, 104.5, 31.0],
        "武汉": [113.8, 29.9, 114.6, 30.8],
        "杭州": [119.9, 30.0, 120.5, 30.5],
        "南京": [118.5, 31.7, 119.2, 32.3],
        "重庆": [105.8, 29.1, 107.2, 30.2],
        "西安": [108.6, 33.9, 109.3, 34.6],
        "天津": [116.7, 38.6, 118.1, 40.2],
        "苏州": [120.3, 31.0, 121.0, 31.6],
        "太湖": [119.8, 30.8, 120.7, 31.6],
        "青岛": [119.8, 35.8, 120.9, 36.8],
    }
    for k, v in quick_bboxes.items():
        if k in place_name:
            return v

    # 若不在预设库中，使用公开在线逆地理接口动态解析
    try:
        url = f"https://nominatim.openstreetmap.org/search?q={place_name}&format=json&limit=1"
        headers = {"User-Agent": "GeoMind-QGIS-Plugin/1.0"}
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200 and r.json():
            res = r.json()[0]
            bb = res.get("boundingbox")  # [min_lat, max_lat, min_lon, max_lon]
            if bb:
                return [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])]
            lat, lon = float(res["lat"]), float(res["lon"])
            return [lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1]
    except Exception:
        pass

    return None


def skill_fetch_sentinel2_imagery(
    place_name: str = "当前视口",
    days_back: int = 14,
    max_cloud: int = 15,
    band_type: str = "4band"  # 默认为 4波段 10m 高清多光谱（含近红外）
) -> str:
    """
    检索并流式加载 Sentinel-2 遥感影像（自动完成地点解析 + 画布定位平移 + 多波段多光谱影像上屏）。
    
    :param place_name: 目标城市或地点名称（如“北京”、“太湖”）
    :param days_back: 回溯天数（默认 14 天）
    :param max_cloud: 允许的最大云量百分比
    :param band_type: 波段组合模式：'4band' (RGB+近红外, 可算NDVI/NDWI), 'full' (6波段含短波红外), 'rgb' (仅真彩色)
    """
    from qgis.core import (
        QgsCoordinateTransform, QgsCoordinateReferenceSystem, 
        QgsProject, QgsRectangle
    )
    from qgis.utils import iface

    bbox = None
    located_msg = ""

    if place_name and place_name not in ("当前视口", "当前视图", "当前区域", "视口"):
        bbox = _geocode_place_bbox(place_name)
        if bbox and iface and iface.mapCanvas():
            canvas = iface.mapCanvas()
            src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            dest_crs = canvas.mapSettings().destinationCrs()
            tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
            target_rect = tr.transformBoundingBox(QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))
            canvas.setExtent(target_rect)
            canvas.refresh()
            located_msg = f"📍 **已自动定位并聚焦地图至**：`{place_name}`\n"

    if not bbox and iface and iface.mapCanvas():
        canvas = iface.mapCanvas()
        rect = canvas.extent()
        src_crs = canvas.mapSettings().destinationCrs()
        dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
        rect_wgs84 = tr.transformBoundingBox(rect)
        bbox = [
            round(rect_wgs84.xMinimum(), 4),
            round(rect_wgs84.yMinimum(), 4),
            round(rect_wgs84.xMaximum(), 4),
            round(rect_wgs84.yMaximum(), 4)
        ]

    if not bbox or (bbox[0] == 0 and bbox[1] == 0):
        bbox = [115.7, 39.4, 117.4, 41.0]

    today = datetime.now()
    start_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    result = search_and_load_sentinel2(
        extent_bbox=bbox,
        date_start=start_date,
        date_end=end_date,
        max_cloud_cover=max_cloud,
        auto_load_first=True,
        band_type=band_type
    )

    return f"{located_msg}{result}"

# ===========================================================================
# 2. Local free tools (delegates to tools.raster_ops)
# ===========================================================================

def skill_calc_spectral_index(layer_name: str, index_type: str, b1_idx: int, b2_idx: int, b3_idx: int = 1) -> str:
    """Calculate a spectral index (NDVI, GNDVI, EVI, NDWI, etc.)."""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        calc_spectral_index(layer.source(), index_type, b1_idx, b2_idx, b3_idx)
        return f"光谱指数 {index_type} 计算成功并已加载。"
    except Exception as e:
        return f"计算失败: {e}"


def skill_run_pca(layer_name: str, n_comp: int = 3) -> str:
    """Run PCA principal component analysis."""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        run_pca(layer.source(), n_comp)
        return f"成功执行 PCA，提取了 {n_comp} 个主成分图层。"
    except Exception as e:
        return f"PCA 分析失败: {e}"


def skill_dem_analysis(layer_name: str, analysis_type: str, z_factor: float = 1.0) -> str:
    """Run DEM terrain analysis (hillshade, slope, aspect, TRI)."""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        dem_analysis(layer.source(), analysis_type, z_factor)
        return f"地形分析 [{analysis_type}] 完成并已加载。"
    except Exception as e:
        return f"地形分析失败: {e}"


def skill_spatial_filter(layer_name: str, filter_type: str, band_idx: int = 1) -> str:
    """Apply spatial filter (sobel, gaussian, laplacian)."""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        spatial_filter(layer.source(), filter_type, band_idx)
        return f"空间滤波 [{filter_type}] 完成！"
    except Exception as e:
        return f"空间滤波失败: {e}"


def skill_area_statistics(layer_name: str) -> str:
    """Compute per-class area statistics (supports both raster and vector layers)."""
    try:
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if not layers:
            raise ValueError(f"找不到图层: '{layer_name}'")
        layer = layers[0]

        # 兼容矢量图层统计
        if isinstance(layer, QgsVectorLayer):
            total_area_m2 = sum(f.geometry().area() for f in layer.getFeatures() if f.hasGeometry())
            total_count = layer.featureCount()
            area_mu = total_area_m2 / 666.6667
            area_sqkm = total_area_m2 / 1_000_000
            return (
                f"矢量图层 '{layer_name}' 面积统计结果：\n"
                f" - 要素总数: {total_count} 个图斑\n"
                f" - 总面积: {total_area_m2:,.2f} 平方米 ({area_mu:,.2f} 亩 / {area_sqkm:.4f} 平方公里)"
            )

        # 栅格图层统计
        stats = area_statistics(layer.source())
        report = [f"栅格图层 '{layer_name}' 像元分类面积统计结果："]
        for s in stats:
            report.append(
                f" - 类别 {s['class_id']}: {s['pixels']} 个像元，"
                f"约 {s['area_m2']:,.2f} 平方米 ({s['area_mu']:,.2f} 亩)"
            )
        return "\n".join(report)
    except Exception as e:
        return f"面积统计失败: {e}"


def skill_vector_smooth(layer_name: str, tolerance: float = 1.0, iterations: int = 2) -> str:
    """Simplify and smooth vector geometries."""
    try:
        layer = get_layer_by_name(layer_name, "vector")
        out_layer = vector_simplify_and_smooth(layer, tolerance, iterations)
        out_layer.setName(f"{layer.name()}_平滑")
        QgsProject.instance().addMapLayer(out_layer)
        return "矢量边界化简与平滑处理完成，已生成新图层。"
    except Exception as e:
        return f"矢量平滑失败: {e}"


def skill_kmeans_cluster(layer_name: str, k: int = 5, max_iters: int = 15) -> str:
    """Run K-Means unsupervised clustering."""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        kmeans_cluster(layer.source(), k, max_iters)
        return f"K-Means (K={k}) 聚类执行成功。"
    except Exception as e:
        return f"K-Means 失败: {e}"


def skill_raster_diff(layer_t1: str, layer_t2: str, threshold: float = 30.0, polygonize: bool = True) -> str:
    """Pixel-level difference detection between two rasters."""
    try:
        l1 = get_layer_by_name(layer_t1, "raster")
        l2 = get_layer_by_name(layer_t2, "raster")
        raster_diff(l1.source(), l2.source(), band_idx=1, threshold=threshold, polygonize=polygonize)
        return "双期影像像元级差分检测成功，已生成变化区域图层。"
    except Exception as e:
        return f"差分检测失败: {e}"


def skill_image_enhance(layer_name: str, r: int = 4, g: int = 3, b: int = 2) -> str:
    """Create false-color composite with enhancement."""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        image_enhance(layer.source(), r, g, b, stretch=True)
        return f"基于波段 {r}-{g}-{b} 的假彩色画质增强已完成。"
    except Exception as e:
        return f"增强失败: {e}"


def skill_raster_polygonize(layer_name: str, sieve_size: int = 4) -> str:
    """Convert raster mask to vector polygons."""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        raster_polygonize(layer.source(), sieve_size)
        return "栅格已成功转换为矢量多边形图斑（并滤除孤立碎斑）。"
    except Exception as e:
        return f"矢量化失败: {e}"


# ===========================================================================
# 3. AI cloud interpretation dispatchers
# ===========================================================================

def skill_ai_extract_feature(
    layer_name: str, feature_type: str, server_url: str, token: str,
    machine_id: str, extent=None, extent_crs=None,
):
    """Create an AI feature extraction InterpretTask."""
    from ..tasks.interpret_task import InterpretTask
    from ..utils.extent_guard import check_extent_too_large
    from ..core.exceptions import ExtentTooLargeError

    layer = get_layer_by_name(layer_name, "raster")
    target_class_id = find_class_ids_by_keywords([feature_type], fallback_id="5")
    real_model_key = get_model_key_by_mode("landuse", fallback_key="LANDUSE")

    task_extent = extent if extent is not None else layer.extent()
    task_extent_crs = extent_crs if extent_crs is not None else layer.crs()

    too_large, guard_msg = check_extent_too_large(layer, task_extent, task_extent_crs)
    if too_large:
        raise ExtentTooLargeError(guard_msg)

    return InterpretTask(
        raster_layer=layer, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class=target_class_id, prompt="",
        output_format="mask", server_url=server_url, machine_id=machine_id, token=token,
    )


def skill_ai_sam3_extract(
    layer_name: str, prompt: str, output_format: str, server_url: str, token: str,
    machine_id: str, extent=None, extent_crs=None,
):
    """Create a SAM3 interpretation InterpretTask."""
    from ..tasks.interpret_task import InterpretTask
    from ..utils.extent_guard import check_extent_too_large
    from ..core.exceptions import ExtentTooLargeError

    layer = get_layer_by_name(layer_name, "raster")
    real_model_key = get_model_key_by_mode("sam3", fallback_key="SAM3_MODEL")

    task_extent = extent if extent is not None else layer.extent()
    task_extent_crs = extent_crs if extent_crs is not None else layer.crs()

    too_large, guard_msg = check_extent_too_large(layer, task_extent, task_extent_crs)
    if too_large:
        raise ExtentTooLargeError(guard_msg)

    return InterpretTask(
        raster_layer=layer, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class="", prompt=prompt,
        output_format=output_format, server_url=server_url, machine_id=machine_id, token=token,
    )


def skill_ai_change_detection(
    layer_t1: str, layer_t2: str, server_url: str, token: str, machine_id: str,
    extent=None, extent_crs=None,
):
    """Create a change detection InterpretTask."""
    from ..tasks.interpret_task import InterpretTask
    from ..utils.extent_guard import check_extent_too_large
    from ..core.exceptions import ExtentTooLargeError

    l1 = get_layer_by_name(layer_t1, "raster")
    l2 = get_layer_by_name(layer_t2, "raster")
    real_model_key = get_model_key_by_mode("change_detection", fallback_key="CHANGE_DETECTION")

    task_extent = extent if extent is not None else l1.extent()
    task_extent_crs = extent_crs if extent_crs is not None else l1.crs()

    too_large, guard_msg = check_extent_too_large(l1, task_extent, task_extent_crs)
    if too_large:
        raise ExtentTooLargeError(guard_msg)

    return InterpretTask(
        raster_layer=l1, raster_layer_after=l2, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class="", prompt="", output_format="mask",
        server_url=server_url, machine_id=machine_id, token=token,
    )


# ===========================================================================
# 4. QGIS Processing algorithm search & execution
# ===========================================================================

def qgis_search_tools(query: str, top_k: int = 5) -> str:
    """Semantic search for QGIS processing algorithms."""
    from ..utils.qgis_indexer import QgisToolVectorIndexer

    indexer = QgisToolVectorIndexer()
    results = indexer.search(query, top_k=top_k)
    if not results:
        return f"未找到与 '{query}' 相关的 QGIS 算子。"

    lines = [f"为您检索到最匹配的 {len(results)} 个 QGIS 算子："]
    for r in results:
        lines.append(
            f"- **ID**: `{r['id']}` | **名称**: {r['name']} ({r['group']}) | 相似度: {r['score']:.2f}"
        )
        lines.append(f"  *描述*: {r['description']}")
    lines.append(
        "\n您可以调用 `qgis_get_tool_params(algorithm_id)` 获取入参详情，"
        "随后调用 `qgis_run_algorithm` 执行。"
    )
    return "\n".join(lines)


def qgis_get_tool_params(algorithm_id: str) -> str:
    """Get the parameter schema for a QGIS processing algorithm."""
    alg = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    if not alg:
        return f"错误：未找到算子 `{algorithm_id}`"

    param_info = [f"算法 `{algorithm_id}` ({alg.displayName()}) 参数列表:"]
    for p in alg.parameterDefinitions():
        req = "必填" if not (p.flags() & QgsProcessingParameterDefinition.FlagOptional) else "选填"
        param_info.append(
            f"- **{p.name()}** ({p.type()}, {req}): {p.description()} (默认值: {p.defaultValue()})"
        )
    return "\n".join(param_info)


def _looks_like_layer_param(alg, param_name: str) -> bool:
    """Check if a parameter expects a layer input."""
    try:
        p = alg.parameterDefinition(param_name)
        if p is None:
            return False
        return p.type() in ("source", "layer", "raster", "vector", "multilayer")
    except Exception:
        return param_name.upper() in ("INPUT", "SOURCE", "LAYER", "LAYER_T1", "LAYER_T2")


def qgis_run_algorithm(algorithm_id: str, parameters: dict) -> str:
    """Execute a QGIS processing algorithm with layer auto-resolution."""
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback

    try:
        registry = QgsApplication.processingRegistry()
        alg = registry.createAlgorithmById(algorithm_id)
        if not alg:
            return f"错误：未找到算法 `{algorithm_id}`"

        resolved_params = {}
        missing_layer_names = []
        for k, v in parameters.items():
            if isinstance(v, str):
                layers = QgsProject.instance().mapLayersByName(v)
                if layers:
                    resolved_params[k] = layers[0]
                elif _looks_like_layer_param(alg, k):
                    missing_layer_names.append((k, v))
                    resolved_params[k] = v
                else:
                    resolved_params[k] = v
            else:
                resolved_params[k] = v

        if missing_layer_names:
            detail = ", ".join(f"{k}='{v}'" for k, v in missing_layer_names)
            return f"参数解析失败：找不到图层 {detail}，请先调用 get_active_layers 确认准确图层名后重试。"

        for out in alg.outputDefinitions():
            if out.name() not in resolved_params:
                resolved_params[out.name()] = "memory:"

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()

        outputs, ok = alg.run(resolved_params, context, feedback)
        if not ok:
            return f"算法 `{alg.displayName()}` ({algorithm_id}) 执行失败：{feedback.textLog() or '无详细日志'}"

        loaded_layers = []
        layers_to_load = dict(context.layersToLoadOnCompletion())
        for layer_id, details in layers_to_load.items():
            layer = context.temporaryLayerStore().mapLayer(layer_id)
            if layer is None:
                layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None and layer.isValid():
                context.temporaryLayerStore().removeMapLayer(layer_id)
                display_name = details.name if getattr(details, "name", None) else layer.name()
                layer.setName(display_name)
                QgsProject.instance().addMapLayer(layer)
                loaded_layers.append(layer.name())

        if not loaded_layers:
            for out_name, out_val in outputs.items():
                layer = None
                if isinstance(out_val, QgsMapLayer):
                    layer = out_val
                elif isinstance(out_val, str) and out_val:
                    layer = context.temporaryLayerStore().mapLayer(out_val)
                    if layer is None and os.path.exists(out_val):
                        lower = out_val.lower()
                        if lower.endswith((".tif", ".tiff", ".img")):
                            cand = QgsRasterLayer(out_val, f"{alg.displayName()}_结果")
                            layer = cand if cand.isValid() else None
                        elif lower.endswith((".shp", ".gpkg", ".geojson")):
                            cand = QgsVectorLayer(out_val, f"{alg.displayName()}_结果", "ogr")
                            layer = cand if cand.isValid() else None

                if layer is not None and layer.isValid():
                    context.temporaryLayerStore().takeMapLayer(layer)
                    QgsProject.instance().addMapLayer(layer)
                    loaded_layers.append(layer.name())

        if not loaded_layers:
            return (
                f"算法 `{alg.displayName()}` ({algorithm_id}) 已调用完成，"
                f"但未捕获到任何有效的输出图层。\n"
                f"原始输出: {json.dumps({k: str(v) for k, v in outputs.items()}, ensure_ascii=False)}\n"
                f"请如实说明结果未确认。"
            )

        return f"算法 `{alg.displayName()}` ({algorithm_id}) 执行成功，已加载图层: {', '.join(loaded_layers)}"

    except Exception as e:
        logger.error("Algorithm execution failed: %s", e)
        return f"执行算子 `{algorithm_id}` 失败: {e}"

def execute_pyqgis_code(python_code: str) -> str:
    """
    【兜底终极技能】在当前 QGIS 环境中直接动态执行 Python / PyQGIS 代码。
    当现有工具箱无法满足复杂定制需求或算子调用失败时使用。
    
    代码环境已默认注入：
    - qgis.core (QgsProject, QgsVectorLayer, QgsRasterLayer, QgsGeometry 等全部核心类)
    - qgis.utils (iface)
    - processing (QGIS 处理工具箱)
    - np (numpy), gdal, os, sys
    
    :param python_code: 完整的 Python/PyQGIS 可执行代码字符串
    """
    import io
    import sys
    import traceback
    import numpy as np
    from osgeo import gdal, ogr, osr
    import processing
    from qgis.utils import iface
    import qgis.core as qgis_core

    # 构建安全且完备的执行命名空间
    exec_globals = {
        "__builtins__": __builtins__,
        "QgsProject": qgis_core.QgsProject,
        "QgsVectorLayer": qgis_core.QgsVectorLayer,
        "QgsRasterLayer": qgis_core.QgsRasterLayer,
        "QgsFeature": qgis_core.QgsFeature,
        "QgsGeometry": qgis_core.QgsGeometry,
        "QgsPointXY": qgis_core.QgsPointXY,
        "QgsRectangle": qgis_core.QgsRectangle,
        "QgsField": qgis_core.QgsField,
        "QgsCoordinateTransform": qgis_core.QgsCoordinateTransform,
        "QgsCoordinateReferenceSystem": qgis_core.QgsCoordinateReferenceSystem,
        "iface": iface,
        "processing": processing,
        "np": np,
        "numpy": np,
        "gdal": gdal,
        "ogr": ogr,
        "osr": osr,
        "os": os,
        "json": json,
    }

    # 捕获脚本中的 print 输出
    stdout_backup = sys.stdout
    captured_output = io.StringIO()
    sys.stdout = captured_output

    try:
        # 执行动态代码
        exec(python_code, exec_globals)
        sys.stdout = stdout_backup
        
        # 刷新画布
        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        logs = captured_output.getvalue().strip()
        log_str = f"\n执行日志输出:\n{logs}" if logs else ""
        return f"✔ PyQGIS 代码执行成功！{log_str}"

    except Exception as e:
        sys.stdout = stdout_backup
        err_detail = traceback.format_exc()
        return f"❌ PyQGIS 代码执行报错: {e}\n详细堆栈:\n{err_detail}"