# -*- coding: utf-8 -*-
"""
LLM skill dispatcher — bridges the Copilot backend to local tools.

整合功能：
1. 图层物理画像探测（区分无人机/高分卫星/哨兵/在线瓦片）；
2. 哨兵二号与 30m 全球 Copernicus DEM 在线流式 STAC 检索；
3. 本地光谱/地形/滤波/聚类分析；
4. 云端 AI 深度解译任务调度；
5. QGIS 原生 Processing 算子检索与防卡死执行；
6. PyQGIS 动态代码防卡死安全沙箱执行器。
"""
import os
import json
import tempfile
import requests
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

from osgeo import gdal, ogr, osr
from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsMapLayer,
    QgsApplication,
    QgsProcessingParameterDefinition,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsRectangle,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsField,
)
from qgis.PyQt.QtCore import QVariant, QCoreApplication
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

logger = get_logger("tools.skill_dispatcher")

STAC_AWS_URL = "https://earth-search.aws.element84.com/v1/search"


# ===========================================================================
# 1. 图层探测与智能画像 (区分无人机/卫星/哨兵/在线瓦片)
# ===========================================================================

def _inspect_raster_profile(layer: QgsRasterLayer) -> Dict[str, Any]:
    """探测栅格影像的物理分辨率与波段数，推断适用的算法路径。"""
    source = layer.source().lower()
    provider = layer.providerType().lower()
    name = layer.name().lower()

    is_online_tile = provider in ("wms", "wmts", "xyz") or "type=xyz" in source
    is_sentinel = "sentinel" in name or "s2_" in source or "sentinel-2" in source

    band_count = layer.bandCount()

    res_x = 999.0
    res_y = 999.0
    if not is_online_tile:
        try:
            ds = gdal.Open(layer.source())
            if ds:
                gt = ds.GetGeoTransform()
                res_x = abs(gt[1])
                res_y = abs(gt[5])
                if layer.crs().isGeographic():
                    res_x *= 111320.0
                    res_y *= 111320.0
                ds = None
        except Exception:
            pass

    if is_online_tile:
        data_type = "在线XYZ/WMTS瓦片 (仅RGB目视，无物理反射率)"
        suggested_route = "【标准地物首选】skill_ai_extract_feature；特殊地物降级使用 SAM3 或 底图参考；【严禁计算物理光谱指数】"
    elif is_sentinel or (band_count >= 4 and 8.0 <= res_x <= 35.0):
        data_type = f"哨兵/中分辨率多光谱卫星 ({res_x:.1f}m 分辨率, {band_count}波段)"
        suggested_route = "【首选光谱指数】skill_calc_spectral_index (如NDVI) + 阈值二值化；或常规地物模型"
    elif res_x < 0.35:
        data_type = f"无人机超高分辨率正射影像 ({res_x*100:.1f}cm 厘米级, {band_count}波段)"
        suggested_route = "【强制首选 SAM3 提示词模型】skill_ai_sam3_extract（地物模型易失效）"
    elif res_x <= 3.5:
        data_type = f"高分辨率卫星影像 ({res_x:.2f}m 分辨率, {band_count}波段)"
        suggested_route = "【标准地物首选】skill_ai_extract_feature；特殊地物降级使用 SAM3"
    else:
        data_type = f"通用高程/栅格数据 ({res_x:.1f}m 分辨率, {band_count}波段)"
        suggested_route = "支持 DEM 地形分析、聚类或滤波算子"

    return {
        "data_type": data_type,
        "band_count": band_count,
        "resolution_m": round(res_x, 3),
        "is_online_tile": is_online_tile,
        "suggested_route": suggested_route,
    }


def get_layer_by_name(layer_name: str, layer_type: str = "raster"):
    """按名称查找图层并进行类型校验。"""
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
    """获取当前工程全部图层的物理属性画像与算法调度建议。"""
    layers = QgsProject.instance().mapLayers().values()
    if not layers:
        return (
            "当前 QGIS 工程中无任何图层。\n"
            "- 若需大范围卫星数据，请调用 `skill_fetch_sentinel2_imagery` 在线拉取哨兵影像；\n"
            "- 若需高程数据，请调用 `skill_fetch_dem_data` 在线拉取 DEM；\n"
            "- 若需超高精解译，请提示用户加载本地无人机影像。"
        )

    info = ["🗺️ **当前 QGIS 工程活动图层画像与调度路由**："]
    for l in layers:
        if isinstance(l, QgsRasterLayer):
            profile = _inspect_raster_profile(l)
            info.append(
                f"- **[栅格图层] `{l.name()}`**\n"
                f"  - 数据类别: `{profile['data_type']}`\n"
                f"  - 推荐处理链: {profile['suggested_route']}"
            )
        elif isinstance(l, QgsVectorLayer):
            geom_type = ["点", "线", "面", "无几何"][min(l.geometryType(), 3)]
            info.append(f"- **[矢量图层] `{l.name()}`**: 类型={geom_type}，要素数={l.featureCount()}个")
    return "\n".join(info)


# ===========================================================================
# 2. 地理编码与多源开放遥感数据在线流式获取
# ===========================================================================

def _geocode_place_bbox(place_name: str) -> Optional[List[float]]:
    """将地名解析为 WGS84 经纬度外包框 [min_lon, min_lat, max_lon, max_lat]"""
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

    try:
        url = f"https://nominatim.openstreetmap.org/search?q={place_name}&format=json&limit=1"
        headers = {"User-Agent": "GeoMind-QGIS-Plugin/1.0"}
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200 and r.json():
            res = r.json()[0]
            bb = res.get("boundingbox")
            if bb:
                return [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])]
            lat, lon = float(res["lat"]), float(res["lon"])
            return [lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1]
    except Exception:
        pass
    return None


def skill_geocode_address(
    address_text: str,
    lon: Optional[float] = None,
    lat: Optional[float] = None,
) -> str:
    """地址地理编码并将 QGIS 画布定位至目标位置。"""
    source_type = "天地图"
    try:
        if iface is None:
            return "错误：获取不到 QGIS iface 对象，无法操作地图画布"

        if lon is not None and lat is not None:
            lon = float(lon)
            lat = float(lat)
            source_type = "地理常识估算/国外定位"
        else:
            if not TIANDITU_API_KEY or len(TIANDITU_API_KEY) < 10:
                return "地图 tk 密钥无效，请配置环境变量 GEOMIND_TIANDITU_TK"

            ds_data = json.dumps({"keyWord": address_text}, ensure_ascii=False)
            params = {"ds": ds_data, "tk": TIANDITU_API_KEY}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.tianditu.gov.cn/",
            }

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
                return f"天地图解析失败: {js.get('msg', '未知错误')}。若是国外地名，请传入 lon/lat。"

            location = js.get("location")
            if not location:
                return f"未匹配到国内地址：'{address_text}'。请评估经纬度后重新调用。"

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
    """底层 AWS STAC 检索 + 按屏幕范围裁剪的虚拟 VRT 流式加载 Sentinel-2 影像。"""
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
        resp = requests.post(STAC_AWS_URL, json=payload, timeout=15)
        if resp.status_code != 200:
            return f"STAC 检索服务异常 (HTTP {resp.status_code})"

        features = resp.json().get("features", [])
        if not features:
            return f"在 {date_start} 至 {date_end} 期间未检索到云量 < {max_cloud_cover}% 的影像，请放宽日期或云量限制。"

        result_lines = [f"🛰️ 成功检索到 {len(features)} 景 Sentinel-2 影像："]
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
                warp_options = gdal.WarpOptions(
                    format="VRT",
                    outputBounds=[extent_bbox[0], extent_bbox[1], extent_bbox[2], extent_bbox[3]],
                    outputBoundsSRS="EPSG:4326",
                    resampleAlg="bilinear"
                )

                if band_type in ("4band", "full") and all(k in assets for k in ("red", "green", "blue", "nir")):
                    band_urls = [
                        f"/vsicurl/{assets['red']['href']}",
                        f"/vsicurl/{assets['green']['href']}",
                        f"/vsicurl/{assets['blue']['href']}",
                        f"/vsicurl/{assets['nir']['href']}",
                    ]
                    if band_type == "full" and "swir16" in assets and "swir22" in assets:
                        band_urls.append(f"/vsicurl/{assets['swir16']['href']}")
                        band_urls.append(f"/vsicurl/{assets['swir22']['href']}")
                        band_desc = "6波段多光谱"
                    else:
                        band_desc = "4波段多光谱(含近红外)"

                    raw_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_raw.vrt")
                    gdal.BuildVRT(raw_vrt, band_urls, options=gdal.BuildVRTOptions(separate=True))

                    clipped_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_screen.vrt")
                    gdal.Warp(clipped_vrt, raw_vrt, options=warp_options)

                    layer_name = f"Sentinel2_{acq_time}_{band_desc}_云量{cloud:.1f}%"
                    layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")

                if layer is None or not layer.isValid():
                    visual_asset = assets.get("visual") or assets.get("overview")
                    if visual_asset:
                        clipped_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_rgb_screen.vrt")
                        gdal.Warp(clipped_vrt, f"/vsicurl/{visual_asset['href']}", options=warp_options)
                        layer_name = f"Sentinel2_{acq_time}_真彩色_云量{cloud:.1f}%"
                        layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")

                if layer and layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    loaded_layer_name = layer_name
                    if iface and iface.mapCanvas():
                        iface.mapCanvas().refresh()

        if loaded_layer_name:
            result_lines.append(f"\n🎉 **已自动为您流式加载影像**：`{loaded_layer_name}`\n"
                                f"📐 *图层范围已自动裁剪为视口范围。*")
        return "\n".join(result_lines)
    except Exception as e:
        return f"检索 Sentinel-2 影像异常: {e}"


def skill_fetch_sentinel2_imagery(
    place_name: str = "当前视口",
    days_back: int = 14,
    max_cloud: int = 15,
    band_type: str = "4band"
) -> str:
    """检索并流式加载 Sentinel-2 遥感影像。"""
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
            located_msg = f"📍 **已自动定位地图至**：`{place_name}`\n"

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


def skill_fetch_dem_data(place_name: str = "当前视口") -> str:
    """检索并流式载入当前视口或指定地点的 30米 Copernicus DEM 高程数据。"""
    try:
        bbox = None
        if place_name and place_name not in ("当前视口", "当前视图", "视口"):
            bbox = _geocode_place_bbox(place_name)

        if not bbox and iface and iface.mapCanvas():
            canvas = iface.mapCanvas()
            rect = canvas.extent()
            src_crs = canvas.mapSettings().destinationCrs()
            dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
            wgs_rect = tr.transformBoundingBox(rect)
            bbox = [wgs_rect.xMinimum(), wgs_rect.yMinimum(), wgs_rect.xMaximum(), wgs_rect.yMaximum()]

        if not bbox:
            return "无法获取视口范围。"

        payload = {
            "collections": ["cop-dem-glo-30"],
            "bbox": bbox,
            "limit": 1
        }
        resp = requests.post(STAC_AWS_URL, json=payload, timeout=12)
        features = resp.json().get("features", [])
        if not features:
            return "当前区域未检索到 Copernicus 30m DEM 数据。"

        data_href = features[0]["assets"]["data"]["href"]
        vsi_url = f"/vsicurl/{data_href}"

        warp_opts = gdal.WarpOptions(
            format="VRT",
            outputBounds=[bbox[0], bbox[1], bbox[2], bbox[3]],
            outputBoundsSRS="EPSG:4326"
        )
        clipped_vrt = os.path.join(tempfile.gettempdir(), f"dem_{datetime.now().strftime('%H%M%S')}.vrt")
        gdal.Warp(clipped_vrt, vsi_url, options=warp_opts)

        layer = QgsRasterLayer(clipped_vrt, f"Copernicus_DEM_30m", "gdal")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return f"✅ 成功流式加载 30米 Copernicus DEM 图层：`{layer.name()}`。"
        return "DEM 图层构建失败。"
    except Exception as e:
        return f"获取 DEM 异常: {e}"


# ===========================================================================
# 3. 栅格与矢量基础算子调度
# ===========================================================================

def skill_calc_spectral_index(layer_name: str, index_type: str, b1_idx: int, b2_idx: int, b3_idx: int = 1) -> str:
    """计算多光谱物理指数 (NDVI, GNDVI, NDWI, EVI 等)。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        profile = _inspect_raster_profile(layer)
        if profile["is_online_tile"]:
            return f"❌ 错误：图层 `{layer_name}` 是在线 XYZ 瓦片（仅 RGB 图像），缺乏物理反射率，严禁计算物理光谱指数！"

        calc_spectral_index(layer.source(), index_type, b1_idx, b2_idx, b3_idx)
        return f"光谱指数 [{index_type.upper()}] 计算成功并已加载至地图。"
    except Exception as e:
        return f"计算失败: {e}"


def skill_raster_threshold(layer_name: str, min_val: float, max_val: float = 1.0, band_idx: int = 1) -> str:
    """对栅格指数或 DEM 执行快速阈值二值化提取（生成 0/1 掩膜）。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            raise RuntimeError(f"无法打开栅格: {layer.source()}")

        arr = ds.GetRasterBand(band_idx).ReadAsArray()
        mask = np.where((arr >= min_val) & (arr <= max_val), 1, 0).astype(np.uint8)

        time_str = datetime.now().strftime("%H%M%S")
        out_file = os.path.join(tempfile.gettempdir(), f"mask_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_file, arr.shape[1], arr.shape[0], 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(mask)
        out_ds.GetRasterBand(1).SetNoDataValue(0)
        out_ds = None
        ds = None

        out_layer = QgsRasterLayer(out_file, f"{layer_name}_阈值提取[{min_val}-{max_val}]")
        if out_layer.isValid():
            QgsProject.instance().addMapLayer(out_layer)
        return f"已成功对 `{layer_name}` 完成阈值提取 [{min_val}, {max_val}]，二值化掩膜已上屏。"
    except Exception as e:
        return f"阈值提取失败: {e}"


def skill_run_pca(layer_name: str, n_comp: int = 3) -> str:
    """PCA 主成分分析。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        run_pca(layer.source(), n_comp)
        return f"成功对 `{layer_name}` 执行 PCA，生成 {n_comp} 个主成分图层。"
    except Exception as e:
        return f"PCA 分析失败: {e}"


def skill_dem_analysis(layer_name: str, analysis_type: str, z_factor: float = 1.0) -> str:
    """DEM 地形特征提取 (hillshade, slope, aspect, TRI)。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        dem_analysis(layer.source(), analysis_type, z_factor)
        return f"地形分析 [{analysis_type}] 执行完成并已加载。"
    except Exception as e:
        return f"地形分析失败: {e}"


def skill_spatial_filter(layer_name: str, filter_type: str, band_idx: int = 1) -> str:
    """空间滤波 (sobel 边缘提取, gaussian 平滑, laplacian 锐化)。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        spatial_filter(layer.source(), filter_type, band_idx)
        return f"空间滤波 [{filter_type}] 处理完成！"
    except Exception as e:
        return f"空间滤波失败: {e}"


def skill_area_statistics(layer_name: str) -> str:
    """统计分类图层面积与占比（同时支持栅格分类与矢量图斑）。"""
    try:
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if not layers:
            raise ValueError(f"找不到图层: '{layer_name}'")
        layer = layers[0]

        if isinstance(layer, QgsVectorLayer):
            total_area_m2 = sum(f.geometry().area() for f in layer.getFeatures() if f.hasGeometry())
            total_count = layer.featureCount()
            area_mu = total_area_m2 / 666.6667
            area_sqkm = total_area_m2 / 1_000_000
            return (
                f"📊 矢量图层 `{layer_name}` 面积统计：\n"
                f"- 要素总数: {total_count} 个图斑\n"
                f"- 总面积: {total_area_m2:,.2f} ㎡ ({area_mu:,.2f} 亩 / {area_sqkm:.4f} k㎡)"
            )

        stats = area_statistics(layer.source())
        report = [f"📊 栅格图层 `{layer_name}` 像元分类面积统计："]
        for s in stats:
            report.append(
                f"- 类别 {s['class_id']}: {s['pixels']} 个像元，约 {s['area_m2']:,.2f} ㎡ ({s['area_mu']:,.2f} 亩，占比 {s.get('percent', 0):.1f}%)"
            )
        return "\n".join(report)
    except Exception as e:
        return f"面积统计失败: {e}"


def skill_vector_smooth(layer_name: str, tolerance: float = 1.0, iterations: int = 2) -> str:
    """矢量边界平滑与化简。"""
    try:
        layer = get_layer_by_name(layer_name, "vector")
        out_layer = vector_simplify_and_smooth(layer, tolerance, iterations)
        out_layer.setName(f"{layer.name()}_平滑")
        QgsProject.instance().addMapLayer(out_layer)
        return f"矢量图层 `{layer_name}` 边界平滑去锯齿完成。"
    except Exception as e:
        return f"矢量平滑失败: {e}"


def skill_kmeans_cluster(layer_name: str, k: int = 5, max_iters: int = 15) -> str:
    """K-Means 聚类。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        kmeans_cluster(layer.source(), k, max_iters)
        return f"K-Means (K={k}) 智能聚类完成。"
    except Exception as e:
        return f"K-Means 失败: {e}"


def skill_raster_diff(layer_t1: str, layer_t2: str, threshold: float = 30.0, polygonize: bool = True) -> str:
    """像元级差分变化检测。"""
    try:
        l1 = get_layer_by_name(layer_t1, "raster")
        l2 = get_layer_by_name(layer_t2, "raster")
        raster_diff(l1.source(), l2.source(), band_idx=1, threshold=threshold, polygonize=polygonize)
        return "双期影像差分变化检测完成，变化掩膜已生成。"
    except Exception as e:
        return f"差分检测失败: {e}"


def skill_image_enhance(layer_name: str, r: int = 4, g: int = 3, b: int = 2) -> str:
    """假彩色合成与画质增强。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        image_enhance(layer.source(), r, g, b, stretch=True)
        return f"基于波段 ({r},{g},{b}) 的画质增强与彩色合成已完成。"
    except Exception as e:
        return f"画质增强失败: {e}"


def skill_raster_polygonize(layer_name: str, sieve_size: int = 4) -> str:
    """栅格转矢量多边形并过滤孤立碎斑。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        raster_polygonize(layer.source(), sieve_size)
        return f"栅格 `{layer_name}` 已成功转换为矢量多边形图斑。"
    except Exception as e:
        return f"矢量化失败: {e}"


# ===========================================================================
# 4. 云端 AI 深度解译任务调度
# ===========================================================================

def skill_ai_extract_feature(
    layer_name: str, feature_type: str, server_url: str, token: str,
    machine_id: str, extent=None, extent_crs=None,
):
    """【卫星影像标准地物模型】建筑/水体/道路/林地/草地/耕地/施工。"""
    from ..tasks.interpret_task import InterpretTask
    from ..utils.extent_guard import check_extent_too_large
    from ..core.exceptions import ExtentTooLargeError

    layer = get_layer_by_name(layer_name, "raster")
    profile = _inspect_raster_profile(layer)

    if profile["resolution_m"] < 0.35:
        logger.warning("无人机厘米级影像建议改用 SAM3。")

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
    """【无人机最高优先 / 万物识别模型】基于 SAM3 的提示词分割与检测。"""
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
    """AI 双期时序变化检测。"""
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
# 5. QGIS 原生 Processing 算子检索与防卡死动态执行
# ===========================================================================

def qgis_search_tools(query: str, top_k: int = 5) -> str:
    """语义搜索 QGIS 原生处理算法。"""
    try:
        from ..utils.qgis_indexer import QgisToolVectorIndexer
        indexer = QgisToolVectorIndexer()
        results = indexer.search(query, top_k=top_k)
        if not results:
            return f"未找到与 '{query}' 相关的 QGIS 原生算子。"

        lines = [f"为您检索到最匹配的 {len(results)} 个 QGIS 算子："]
        for r in results:
            lines.append(f"- **ID**: `{r['id']}` | **名称**: {r['name']} ({r['group']})")
            lines.append(f"  *说明*: {r['description']}")
        lines.append("\n可调用 `qgis_get_tool_params(algorithm_id)` 获取入参，随后调用 `qgis_run_algorithm` 执行。")
        return "\n".join(lines)
    except Exception as e:
        return f"检索算子失败: {e}"


def qgis_get_tool_params(algorithm_id: str) -> str:
    """获取指定算法的入参 Schema。"""
    alg = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    if not alg:
        return f"错误：未找到算子 `{algorithm_id}`"

    param_info = [f"算法 `{algorithm_id}` ({alg.displayName()}) 参数列表:"]
    for p in alg.parameterDefinitions():
        req = "必填" if not (p.flags() & QgsProcessingParameterDefinition.FlagOptional) else "选填"
        param_info.append(f"- **{p.name()}** ({p.type()}, {req}): {p.description()} (默认: {p.defaultValue()})")
    return "\n".join(param_info)


def _looks_like_layer_param(alg, param_name: str) -> bool:
    try:
        p = alg.parameterDefinition(param_name)
        if p is None:
            return False
        return p.type() in ("source", "layer", "raster", "vector", "multilayer")
    except Exception:
        return param_name.upper() in ("INPUT", "SOURCE", "LAYER", "LAYER_T1", "LAYER_T2")


def qgis_run_algorithm(algorithm_id: str, parameters: dict) -> str:
    """执行原生 QGIS 算法并自动加载结果（内置画布冻结与防卡死保护）。"""
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback

    canvas = iface.mapCanvas() if iface else None
    if canvas:
        canvas.setRenderFlag(False)

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
            return f"参数错误：未找到图层 {detail}，请调用 get_active_layers 确认准确图层名。"

        for out in alg.outputDefinitions():
            if out.name() not in resolved_params:
                resolved_params[out.name()] = "memory:"

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()

        QCoreApplication.processEvents()

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
            return f"算法 `{alg.displayName()}` 执行完成，但未捕获到有效图层。"

        return f"算法 `{alg.displayName()}` 执行成功，已加载图层: {', '.join(loaded_layers)}"
    except Exception as e:
        logger.error("Algorithm execution failed: %s", e)
        return f"执行算子 `{algorithm_id}` 失败: {e}"
    finally:
        if canvas:
            canvas.setRenderFlag(True)
            canvas.refresh()
        QCoreApplication.processEvents()


# ===========================================================================
# 6. PyQGIS 动态代码执行器 (防假死与内存沙箱强化版)
# ===========================================================================

def execute_pyqgis_code(python_code: str) -> str:
    """
    【防卡死安全强化版】在当前 QGIS 环境中动态执行 Python/PyQGIS 代码。
    """
    import io
    import sys
    import traceback
    import processing
    import qgis.core as qgis_core

    canvas = iface.mapCanvas() if iface else None
    if canvas:
        canvas.setRenderFlag(False)

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
        "QgsFeatureRequest": qgis_core.QgsFeatureRequest,
        "QgsSpatialIndex": qgis_core.QgsSpatialIndex,
        "edit": qgis_core.edit,
        "processEvents": QCoreApplication.processEvents,
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

    stdout_backup = sys.stdout
    captured_output = io.StringIO()
    sys.stdout = captured_output

    try:
        QCoreApplication.processEvents()
        exec(python_code, exec_globals)
        sys.stdout = stdout_backup

        logs = captured_output.getvalue().strip()
        log_str = f"\n执行日志输出:\n{logs}" if logs else ""
        return f"✔ PyQGIS 代码执行成功！{log_str}"

    except Exception as e:
        sys.stdout = stdout_backup
        err_detail = traceback.format_exc()
        return f"❌ PyQGIS 代码执行报错: {e}\n详细堆栈:\n{err_detail}"

    finally:
        if canvas:
            canvas.setRenderFlag(True)
            canvas.refresh()
        QCoreApplication.processEvents()