# -*- coding: utf-8 -*-
"""
LLM skill dispatcher — bridges the Copilot backend to local tools.

整合功能：
1. 图层物理画像探测（区分无人机/高分卫星/哨兵/在线瓦片）；
2. 哨兵二号与 30m 全球 Copernicus DEM 在线流式 STAC 检索；
3. 本地光谱/地形/滤波/聚类分析；
4. 云端 AI 深度解译任务调度；
5. QGIS 原生 Processing 算子检索与防卡死执行；
6. PyQGIS 动态代码防卡死安全沙箱执行器；
7. OpenStreetMap 与全球多源开放数据接入（Natural Earth, Landsat, WorldCover 等）；
8. 实时联网搜索与网页正文解析。
"""
import os
import re
import json
import math
import tempfile
import urllib.parse
from html import unescape
from io import BytesIO
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any, List

import requests
import numpy as np
from PIL import Image
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
# 2. 地理编码与多源遥感数据真实外包框拉取
# ===========================================================================

def _validate_bbox(bbox: List[float]) -> List[float]:
    """保证 BBox 处于合法全球经纬度范围内 [-180, 180], [-90, 90]。"""
    min_lon = max(-180.0, min(180.0, float(bbox[0])))
    min_lat = max(-90.0, min(90.0, float(bbox[1])))
    max_lon = max(-180.0, min(180.0, float(bbox[2])))
    max_lat = max(-90.0, min(90.0, float(bbox[3])))
    return [round(min_lon, 6), round(min_lat, 6), round(max_lon, 6), round(max_lat, 6)]


def _geocode_place_bbox(place_name: str) -> Optional[List[float]]:
    """
    将地名解析为真实地理范围外包框 [min_lon, min_lat, max_lon, max_lat]。
    """
    quick_bboxes = {
        "北京": [115.42, 39.44, 117.51, 41.06],
        "上海": [120.86, 30.67, 122.20, 31.87],
        "广州": [112.96, 22.52, 114.05, 23.93],
        "深圳": [113.75, 22.44, 114.63, 22.86],
        "成都": [102.99, 30.09, 104.89, 31.44],
        "武汉": [113.68, 29.97, 115.08, 31.36],
        "杭州": [118.35, 29.19, 120.73, 30.56],
        "南京": [118.36, 31.23, 119.24, 32.61],
        "重庆": [105.29, 28.16, 110.19, 32.20],
        "西安": [107.67, 33.70, 109.82, 34.75],
        "天津": [116.71, 38.56, 118.06, 40.25],
        "苏州": [119.92, 30.78, 121.37, 32.04],
        "太湖": [119.89, 30.92, 120.60, 31.55],
        "青岛": [119.50, 35.58, 121.01, 37.15],
    }
    for k, v in quick_bboxes.items():
        if place_name in (k, f"{k}市"):
            return _validate_bbox(v)

    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": place_name, "format": "json", "limit": 1}
        headers = {"User-Agent": "GeoMind-QGIS-Plugin/1.0"}
        r = requests.get(url, params=params, headers=headers, timeout=5)
        if r.status_code == 200 and r.json():
            res = r.json()[0]
            bb = res.get("boundingbox")
            if bb and len(bb) == 4:
                parsed_bbox = [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])]
                return _validate_bbox(parsed_bbox)
            lat, lon = float(res["lat"]), float(res["lon"])
            # 若无外包框返回则生成约 5km 基础视窗
            half = 0.025
            return _validate_bbox([lon - half, lat - half, lon + half, lat + half])
    except Exception:
        pass
    return None


def _get_target_bbox(place_name: str = "当前视口") -> Tuple[List[float], str]:
    """
    【空间视口提取】
    1. 若指定地名：解析坐标并定位到该地名真实范围；
    2. 若未指定地名：精确提取当前 QGIS 画布真实的可见范围矩形，不做多余截断。
    """
    canvas = iface.mapCanvas() if iface else None
    located_msg = ""
    bbox = None

    # 1. 用户明确指定了具体地名
    is_explicit_place = place_name and place_name not in (
        "当前视口", "当前视图", "当前区域", "视口", "current", "", None
    )

    if is_explicit_place:
        bbox = _geocode_place_bbox(place_name)
        if bbox:
            located_msg = f"📍 已匹配目标地名范围：`{place_name}`\n"
            if canvas:
                src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
                dest_crs = canvas.mapSettings().destinationCrs()
                tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
                rect = tr.transformBoundingBox(QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))
                canvas.setExtent(rect)
                canvas.refresh()
        else:
            located_msg = f"⚠️ 未能解析地名 `{place_name}`，已使用当前屏幕视口\n"

    # 2. 从当前屏幕视口提取物理矩形（不缩放、不截断，所见即所得）
    if bbox is None and canvas:
        rect = canvas.extent()
        src_crs = canvas.mapSettings().destinationCrs()
        dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
        wgs_rect = tr.transformBoundingBox(rect)

        bbox = [
            round(wgs_rect.xMinimum(), 6),
            round(wgs_rect.yMinimum(), 6),
            round(wgs_rect.xMaximum(), 6),
            round(wgs_rect.yMaximum(), 6),
        ]

    # 兜底默认值（防止无图层且画布异常）
    if not bbox or (bbox[0] == 0 and bbox[1] == 0):
        bbox = [108.92, 34.23, 108.98, 34.29]

    final_bbox = _validate_bbox(bbox)
    return final_bbox, located_msg


def skill_geocode_address(
    address_text: str,
    lon: Optional[float] = None,
    lat: Optional[float] = None,
    zoom_scale: float = 6000.0,
) -> str:
    """
    地址地理编码并将 QGIS 画布精细聚焦至目标设施/园区。
    """
    source_type = "天地图"
    try:
        if iface is None:
            return "错误：获取不到 QGIS iface 对象，无法操作地图画布"

        if lon is not None and lat is not None:
            lon = float(lon)
            lat = float(lat)
            source_type = "坐标精确定位"
        else:
            if not TIANDITU_API_KEY or len(TIANDITU_API_KEY) < 10:
                quick_bbox = _geocode_place_bbox(address_text)
                if quick_bbox:
                    lon = (quick_bbox[0] + quick_bbox[2]) / 2.0
                    lat = (quick_bbox[1] + quick_bbox[3]) / 2.0
                    source_type = "内置核心地名库"
                else:
                    return "地图 tk 密钥无效且未匹配到内置地名，请配置环境变量 GEOMIND_TIANDITU_TK 或直接传入 lon/lat。"
            else:
                ds_data = json.dumps({"keyWord": address_text}, ensure_ascii=False)
                params = {"ds": ds_data, "tk": TIANDITU_API_KEY}
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.tianditu.gov.cn/",
                }

                from ..api.http_client import HttpClient
                client = HttpClient(request_timeout=10, retries=1)
                try:
                    resp = client.get(
                        TIANDITU_GEOCODER_URL,
                        params=params,
                        headers=headers,
                        timeout=10,
                        retry_on_status=True,
                    )
                    if not resp.text or not resp.text.strip():
                        raise ValueError("天地图返回空响应")
                    js = resp.json()
                except Exception as e_json:
                    logger.warning(f"天地图接口异常 ({e_json})，启动备用解析")
                    quick_bbox = _geocode_place_bbox(address_text)
                    if quick_bbox:
                        lon = (quick_bbox[0] + quick_bbox[2]) / 2.0
                        lat = (quick_bbox[1] + quick_bbox[3]) / 2.0
                        source_type = "备用地理库"
                    else:
                        return f"天地图解析失败: {e_json}。请直接传入 lon/lat 坐标。"
                finally:
                    client.close()

                if source_type == "天地图":
                    if js.get("status") != "0":
                        return f"天地图解析失败: {js.get('msg', '未知错误')}。请传入 lon/lat。"
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
        canvas.refresh()
        return f"📍 地址定位完成：{address_text} (经度={lon:.6f}, 纬度={lat:.6f})。"
    except Exception as e:
        logger.error("Geocode failed: %s", e)
        return f"地址解析/定位失败: {e}"


def search_and_load_sentinel2(
    extent_bbox: list,
    date_start: str = None,
    date_end: str = None,
    max_cloud_cover: int = 15,
    auto_load_first: bool = True
) -> str:
    """底层 AWS STAC 检索 + 按目标区域裁剪的虚拟 VRT 流式加载 Sentinel-2 全波段影像。"""
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

    # 定义全波段顺序及兼容的 STAC Asset Key 别名
    SENTINEL2_FULL_BANDS = [
        ("B01", ["coastal", "B01", "b01"]),
        ("B02", ["blue", "B02", "b02"]),
        ("B03", ["green", "B03", "b03"]),
        ("B04", ["red", "B04", "b04"]),
        ("B05", ["rededge1", "B05", "b05"]),
        ("B06", ["rededge2", "B06", "b06"]),
        ("B07", ["rededge3", "B07", "b07"]),
        ("B08", ["nir", "nir08", "B08", "b08"]),
        ("B8A", ["nir09", "nir_narrow", "B8A", "b8a"]),
        ("B09", ["wvp", "water_vapour", "B09", "b09"]),
        ("B11", ["swir16", "swir1", "B11", "b11"]),
        ("B12", ["swir22", "swir2", "B12", "b12"]),
    ]

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
                # 1. 匹配并按顺序提取所有波段的 URL
                band_urls = []
                matched_band_names = []
                for band_name, aliases in SENTINEL2_FULL_BANDS:
                    for alias in aliases:
                        if alias in assets and "href" in assets[alias]:
                            band_urls.append(f"/vsicurl/{assets[alias]['href']}")
                            matched_band_names.append(band_name)
                            break

                if not band_urls:
                    result_lines.append(f"⚠️ 无法在影像 `{item_id}` 中匹配到光谱波段资源。")
                    continue

                # 2. 通过 GDAL BuildVRT 合并全波段（separate=True 实现多波段堆叠）
                raw_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_full_raw.vrt")
                gdal.BuildVRT(
                    raw_vrt,
                    band_urls,
                    options=gdal.BuildVRTOptions(separate=True, resolution="highest")
                )

                # 3. 按目标区域裁剪
                warp_options = gdal.WarpOptions(
                    format="VRT",
                    outputBounds=[extent_bbox[0], extent_bbox[1], extent_bbox[2], extent_bbox[3]],
                    outputBoundsSRS="EPSG:4326",
                    resampleAlg="bilinear"
                )
                clipped_vrt = os.path.join(tempfile.gettempdir(), f"s2_{item_id}_full_screen.vrt")
                gdal.Warp(clipped_vrt, raw_vrt, options=warp_options)

                # 4. 加载到 QGIS
                layer_name = f"Sentinel2_{acq_time}_全波段({len(band_urls)}B)_云量{cloud:.1f}%"
                layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")

                if layer and layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    loaded_layer_name = layer_name
                    if 'iface' in globals() and iface and iface.mapCanvas():
                        iface.mapCanvas().refresh()

        if loaded_layer_name:
            result_lines.append(f"\n🎉 **已自动为您流式加载全波段影像**：`{loaded_layer_name}`")
        return "\n".join(result_lines)
    except Exception as e:
        return f"检索 Sentinel-2 影像异常: {e}"

def skill_fetch_sentinel2_imagery(
        place_name: str = "当前视口",
        date_start: str = None,
        date_end: str = None,
        days_back: int = 14,
        max_cloud: int = 15,
        **kwargs  # 兼容吸收可能传入的 band_type 等历史参数，防止抛出 TypeError
) -> str:
    """检索并流式加载 Sentinel-2 12波段全光谱遥感影像。"""
    # 1. 解析目标区域坐标与定位提示信息
    bbox, located_msg = _get_target_bbox(place_name)

    # 2. 解析起止日期
    today = datetime.now()
    end_date = date_end or today.strftime("%Y-%m-%d")
    start_date = date_start or (today - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # 3. 调用底层全波段检索与 VRT 加载引擎
    result = search_and_load_sentinel2(
        extent_bbox=bbox,
        date_start=start_date,
        date_end=end_date,
        max_cloud_cover=max_cloud,
        auto_load_first=True
    )

    return f"{located_msg}{result}"


# ---------------------------------------------------------------------------
# 辅助函数：经纬度与高程瓦片编号换算
# ---------------------------------------------------------------------------
def _deg2num(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


def _num2deg(xtile: int, ytile: int, zoom: int) -> Tuple[float, float]:
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg


def _download_and_decode_terrarium_tif(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float, out_tif_path: str
) -> bool:
    """自动抓取 AWS Terrarium 高程瓦片并解码为真实单波段 Float32 GeoTIFF 栅格"""
    try:
        span = max(max_lon - min_lon, max_lat - min_lat)
        if span > 1.0:
            zoom = 10
        elif span > 0.4:
            zoom = 11
        elif span > 0.15:
            zoom = 12
        else:
            zoom = 13

        x_min, y_min = _deg2num(max_lat, min_lon, zoom)
        x_max, y_max = _deg2num(min_lat, max_lon, zoom)

        cols = x_max - x_min + 1
        rows = y_max - y_min + 1
        tile_size = 256

        full_image = np.zeros((rows * tile_size, cols * tile_size, 3), dtype=np.uint8)
        headers = {"User-Agent": "GeoMind-QGIS-Plugin/1.0"}

        for j, y in enumerate(range(y_min, y_max + 1)):
            for i, x in enumerate(range(x_min, x_max + 1)):
                url = f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{zoom}/{x}/{y}.png"
                r = requests.get(url, headers=headers, timeout=8)
                if r.status_code == 200:
                    tile_img = np.array(Image.open(BytesIO(r.content)).convert("RGB"))
                    full_image[j*tile_size:(j+1)*tile_size, i*tile_size:(i+1)*tile_size] = tile_img

        R = full_image[:, :, 0].astype(np.float32)
        G = full_image[:, :, 1].astype(np.float32)
        B = full_image[:, :, 2].astype(np.float32)
        dem_array = (R * 256.0 + G + B / 256.0) - 32768.0

        top_lat, left_lon = _num2deg(x_min, y_min, zoom)
        bottom_lat, right_lon = _num2deg(x_max + 1, y_max + 1, zoom)

        h, w = dem_array.shape
        driver = gdal.GetDriverByName("GTiff")
        ds = driver.Create(out_tif_path, w, h, 1, gdal.GDT_Float32)

        res_x = (right_lon - left_lon) / w
        res_y = (top_lat - bottom_lat) / h
        ds.SetGeoTransform([left_lon, res_x, 0, top_lat, 0, -res_y])

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        ds.SetProjection(srs.ExportToWkt())

        ds.GetRasterBand(1).WriteArray(dem_array)
        ds.FlushCache()
        ds = None
        return True
    except Exception as e:
        logger.error(f"Terrarium DEM decoding failed: {e}")
        return False


def skill_fetch_dem_data(place_name: str = "当前视口", dem_type: str = "COP30") -> str:
    """
    检索并下载当前视口或指定区域的高精度 30米 全球真实 DEM 高程栅格 (GeoTIFF)。
    """
    try:
        bbox, located_msg = _get_target_bbox(place_name)
        min_lon, min_lat, max_lon, max_lat = bbox

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"dem_real_{dem_type}_{time_str}.tif")

        # 通道 1：OpenTopography API
        try:
            ot_url = "https://portal.opentopography.org/API/globaldem"
            params = {
                "demtype": dem_type if dem_type in ("COP30", "SRTMGL1", "NASADEM") else "COP30",
                "south": min_lat,
                "north": max_lat,
                "west": min_lon,
                "east": max_lon,
                "outputFormat": "GTiff"
            }
            resp = requests.get(ot_url, params=params, timeout=10)
            if resp.status_code == 200 and len(resp.content) > 10000 and resp.content[:4] in (
                b'II*\x00', b'MM\x00*', b'II\x2b\x00'
            ):
                with open(out_tif, "wb") as f:
                    f.write(resp.content)

                layer_name = f"Copernicus_DEM_30m_{time_str}"
                layer = QgsRasterLayer(out_tif, layer_name, "gdal")
                if layer.isValid():
                    QgsProject.instance().addMapLayer(layer)
                    return f"{located_msg}⛰️ **已成功下载并加载 30米 高精 DEM 图层**：`{layer_name}`\n*(数据源: Copernicus GLO-30 / OpenTopography)*"
        except Exception as e_ot:
            logger.warning(f"OpenTopography DEM channel fallback: {e_ot}")

        # 通道 2：AWS Earth Search STAC
        try:
            payload = {
                "collections": ["cop-dem-glo-30"],
                "bbox": [min_lon, min_lat, max_lon, max_lat],
                "limit": 1
            }
            resp = requests.post(STAC_AWS_URL, json=payload, timeout=8)
            if resp.status_code == 200:
                features = resp.json().get("features", [])
                if features:
                    assets = features[0].get("assets", {})
                    target_asset = (
                        assets.get("data") or assets.get("elevation") or assets.get("dem")
                    )
                    if target_asset and "href" in target_asset:
                        href = target_asset["href"]
                        if href.startswith("s3://"):
                            href = href.replace("s3://", "https://s3.amazonaws.com/")
                        vsi_url = f"/vsicurl/{href}"

                        warp_opts = gdal.WarpOptions(
                            format="VRT",
                            outputBounds=[min_lon, min_lat, max_lon, max_lat],
                            outputBoundsSRS="EPSG:4326",
                            resampleAlg="bilinear"
                        )
                        clipped_vrt = os.path.join(tempfile.gettempdir(), f"dem_stac_{time_str}.vrt")
                        gdal.Warp(clipped_vrt, vsi_url, options=warp_opts)

                        layer_name = f"Copernicus_DEM_30m_{time_str}"
                        layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")
                        if layer.isValid():
                            QgsProject.instance().addMapLayer(layer)
                            return f"{located_msg}⛰️ **已成功流式加载 30米 Copernicus DEM 图层**：`{layer_name}`"
        except Exception as e_stac:
            logger.warning(f"STAC DEM channel fallback: {e_stac}")

        # 通道 3：AWS Terrarium 瓦片解码 + 按真实范围裁切
        raw_dem_tif = os.path.join(tempfile.gettempdir(), f"raw_dem_{time_str}.tif")
        ok = _download_and_decode_terrarium_tif(min_lon, min_lat, max_lon, max_lat, raw_dem_tif)
        if ok:
            warp_opts = gdal.WarpOptions(
                format="GTiff",
                outputBounds=[min_lon, min_lat, max_lon, max_lat],
                outputBoundsSRS="EPSG:4326",
                resampleAlg="bilinear"
            )
            gdal.Warp(out_tif, raw_dem_tif, options=warp_opts)
            try:
                os.remove(raw_dem_tif)
            except Exception:
                pass

            layer_name = f"Global_DEM_30m_{time_str}"
            layer = QgsRasterLayer(out_tif, layer_name, "gdal")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                return (
                    f"{located_msg}⛰️ **已通过高程瓦片解码成功生成真实 DEM 栅格**：`{layer_name}`\n"
                    f"💡 *该栅格严格对齐当前工作区范围，完全支持坡度、坡向、填洼与积水区分析算子。*"
                )

        return f"{located_msg}❌ 获取 DEM 失败：所有在线通道与瓦片解码均未完成，请检查网络。"

    except Exception as e:
        return f"获取 DEM 异常: {e}"


# ===========================================================================
# 3. 栅格与矢量基础算子调度
# ===========================================================================

# def skill_calc_spectral_index(
#     layer_name: str,
#     index_type: str,
#     custom_bands: dict = None
# ) -> str:
#     """
#     【PyQGIS 原生光谱指数计算引擎】
#     基于 Sentinel-2 12个标准全波段顺序（或自定义映射）自动提取波段并计算物理光谱指数。
#
#     默认波段顺序映射（Sentinel-2 12-Band）：
#     - Band 2: Blue (490nm)       | Band 3: Green (560nm)
#     - Band 4: Red (665nm)        | Band 5: RedEdge1 (705nm)
#     - Band 8: NIR (842nm)        | Band 11: SWIR1 (1610nm)
#     - Band 12: SWIR2 (2190nm)
#
#     支持指数：
#     - NDVI  (归一化植被): (NIR - Red) / (NIR + Red) -> (B8 - B4)
#     - NDWI  (水体指数):   (Green - NIR) / (Green + NIR) -> (B3 - B8)
#     - MNDWI (修正水体):   (Green - SWIR1) / (Green + SWIR1) -> (B3 - B11)
#     - NDBI  (建筑物指数): (SWIR1 - NIR) / (SWIR1 + NIR) -> (B11 - B8)
#     - GNDVI (绿度植被):   (NIR - Green) / (NIR + Green) -> (B8 - B3)
#     - NDRE  (红边植被):   (NIR - RedEdge1) / (NIR + RedEdge1) -> (B8 - B5)
#     - NBR   (燃烧痕迹):   (NIR - SWIR2) / (NIR + SWIR2) -> (B8 - B12)
#     - EVI   (增强植被):   2.5 * (NIR - Red) / (NIR + 6*Red - 7.5*Blue + 1)
#     """
#     try:
#         layer = get_layer_by_name(layer_name, "raster")
#         profile = _inspect_raster_profile(layer)
#         if profile["is_online_tile"]:
#             return f"❌ 错误：图层 `{layer_name}` 是在线瓦片（RGB），无多光谱物理反射率，无法计算物理指数！"
#
#         # 1. 打开栅格
#         ds = gdal.Open(layer.source())
#         if ds is None:
#             return f"❌ 无法打开栅格数据源: {layer.source()}"
#
#         max_b = ds.RasterCount
#         idx_type = index_type.strip().upper()
#
#         # 2. 确定波段顺序映射（默认使用 12 波段全光谱顺序，1-based）
#         default_bands = {
#             "BLUE": 2,
#             "GREEN": 3,
#             "RED": 4,
#             "RE1": 5,
#             "NIR": 8,
#             "SWIR1": 11,
#             "SWIR2": 12
#         }
#         band_map = custom_bands if custom_bands else default_bands
#
#         def read_band(b_name):
#             b_idx = band_map.get(b_name)
#             if b_idx is None or b_idx > max_b or b_idx < 1:
#                 raise ValueError(f"图层缺少计算所需的 {b_name} 波段（要求索引 {b_idx}，图层总波段数 {max_b}）")
#             return ds.GetRasterBand(b_idx).ReadAsArray().astype(np.float32)
#
#         eps = 1e-6
#
#         # 3. 根据标准公式进行多波段矩阵计算
#         with np.errstate(divide='ignore', invalid='ignore'):
#             if idx_type == "NDVI":
#                 nir, red = read_band("NIR"), read_band("RED")
#                 denom = nir + red
#                 result = np.where(np.abs(denom) < eps, 0.0, (nir - red) / (denom + eps))
#
#             elif idx_type == "NDWI":
#                 green, nir = read_band("GREEN"), read_band("NIR")
#                 denom = green + nir
#                 result = np.where(np.abs(denom) < eps, 0.0, (green - nir) / (denom + eps))
#
#             elif idx_type == "MNDWI":
#                 green, swir1 = read_band("GREEN"), read_band("SWIR1")
#                 denom = green + swir1
#                 result = np.where(np.abs(denom) < eps, 0.0, (green - swir1) / (denom + eps))
#
#             elif idx_type == "GNDVI":
#                 nir, green = read_band("NIR"), read_band("GREEN")
#                 denom = nir + green
#                 result = np.where(np.abs(denom) < eps, 0.0, (nir - green) / (denom + eps))
#
#             elif idx_type == "NDBI":
#                 swir1, nir = read_band("SWIR1"), read_band("NIR")
#                 denom = swir1 + nir
#                 result = np.where(np.abs(denom) < eps, 0.0, (swir1 - nir) / (denom + eps))
#
#             elif idx_type == "NDRE":
#                 nir, re1 = read_band("NIR"), read_band("RE1")
#                 denom = nir + re1
#                 result = np.where(np.abs(denom) < eps, 0.0, (nir - re1) / (denom + eps))
#
#             elif idx_type == "NBR":
#                 nir, swir2 = read_band("NIR"), read_band("SWIR2")
#                 denom = nir + swir2
#                 result = np.where(np.abs(denom) < eps, 0.0, (nir - swir2) / (denom + eps))
#
#             elif idx_type == "EVI":
#                 nir, red, blue = read_band("NIR"), read_band("RED"), read_band("BLUE")
#                 denom = nir + 6.0 * red - 7.5 * blue + 1.0
#                 result = np.where(np.abs(denom) < eps, 0.0, 2.5 * (nir - red) / (denom + eps))
#
#             else:
#                 return f"❌ 不支持的光谱指数类型: `{idx_type}`。支持列表: NDVI, NDWI, MNDWI, GNDVI, NDBI, NDRE, NBR, EVI。"
#
#             # 异常值清洗与归一化范围限制
#             result = np.nan_to_num(result, nan=0.0, posinf=1.0, neginf=-1.0)
#             if idx_type in ("NDVI", "NDWI", "MNDWI", "GNDVI", "NDBI", "NDRE", "NBR"):
#                 result = np.clip(result, -1.0, 1.0)
#
#         # 4. 输出为临时 Float32 GeoTIFF
#         time_str = datetime.now().strftime("%H%M%S")
#         out_tif_path = os.path.join(tempfile.gettempdir(), f"{idx_type}_{time_str}.tif")
#
#         driver = gdal.GetDriverByName("GTiff")
#         h, w = result.shape
#         out_ds = driver.Create(out_tif_path, w, h, 1, gdal.GDT_Float32)
#         out_ds.SetGeoTransform(ds.GetGeoTransform())
#         out_ds.SetProjection(ds.GetProjection())
#
#         band = out_ds.GetRasterBand(1)
#         band.WriteArray(result)
#         band.SetNoDataValue(-9999.0)
#         band.FlushCache()
#         out_ds = None
#         ds = None
#
#         # 5. 加载至 QGIS 画布
#         out_layer_name = f"{idx_type}_{layer_name}"
#         new_layer = QgsRasterLayer(out_tif_path, out_layer_name, "gdal")
#
#         if not new_layer.isValid():
#             return f"❌ 指数生成完成，但加载至 QGIS 失败。"
#
#         QgsProject.instance().addMapLayer(new_layer)
#         if 'iface' in globals() and iface and iface.mapCanvas():
#             iface.mapCanvas().refresh()
#
#         return (
#             f"🎉 **光谱指数 [{idx_type}] 计算成功并已加载**：`{out_layer_name}`\n"
#             f"💡 *目标地物高值区间通常映射在 (0, 1]，可直接调用阈值提取工具提取目标区域。*"
#         )
#
#     except Exception as e:
#         if 'logger' in globals():
#             logger.error(f"Calculate spectral index error: {e}")
#         return f"光谱指数计算失败: {e}"

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


# ===========================================================================
# 3. 栅格与矢量核心算法引擎 (纯 PyQGIS / GDAL / NumPy 自包含原生实现)
# ===========================================================================

def skill_run_pca(layer_name: str, n_comp: int = 3) -> str:
    """【PyQGIS 原生】PCA 多波段主成分分析。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        band_count = ds.RasterCount
        if band_count < 2:
            return f"❌ PCA 分析至少需要 2 个波段，当前图层仅有 {band_count} 个波段。"

        # 读取全部波段数据
        bands_data = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(band_count)]
        h, w = bands_data[0].shape
        X = np.stack([b.flatten() for b in bands_data], axis=1)

        # 协方差矩阵与特征分解
        mean = np.mean(X, axis=0)
        X_centered = X - mean
        cov = np.cov(X_centered, rowvar=False)

        eig_vals, eig_vecs = np.linalg.eigh(cov)
        sort_indices = np.argsort(eig_vals)[::-1]
        eig_vecs = eig_vecs[:, sort_indices]

        actual_comp = min(n_comp, band_count)
        loaded_layers = []
        time_str = datetime.now().strftime("%H%M%S")

        driver = gdal.GetDriverByName("GTiff")
        for i in range(actual_comp):
            pc_arr = np.dot(X_centered, eig_vecs[:, i]).reshape((h, w)).astype(np.float32)
            out_tif = os.path.join(tempfile.gettempdir(), f"PCA_PC{i + 1}_{time_str}.tif")

            out_ds = driver.Create(out_tif, w, h, 1, gdal.GDT_Float32)
            out_ds.SetGeoTransform(ds.GetGeoTransform())
            out_ds.SetProjection(ds.GetProjection())
            out_ds.GetRasterBand(1).WriteArray(pc_arr)
            out_ds.FlushCache()
            out_ds = None

            pc_layer_name = f"{layer_name}_PCA_PC{i + 1}"
            lyr = QgsRasterLayer(out_tif, pc_layer_name, "gdal")
            if lyr.isValid():
                QgsProject.instance().addMapLayer(lyr)
                loaded_layers.append(pc_layer_name)

        ds = None
        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        return f"🎉 成功对 `{layer_name}` 完成 PCA 分析，已生成并加载 {len(loaded_layers)} 个主成分图层：\n- " + "\n- ".join(loaded_layers)
    except Exception as e:
        return f"PCA 分析失败: {e}"


def skill_dem_analysis(layer_name: str, analysis_type: str = "hillshade", z_factor: float = 1.0) -> str:
    """【PyQGIS 原生】DEM 地形特征提取 (hillshade/山体阴影, slope/坡度, aspect/坡向, TRI/崎岖度)。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"dem_{analysis_type}_{time_str}.tif")
        opt_type = analysis_type.lower().strip()

        if opt_type in ("hillshade", "阴影", "山体阴影"):
            gdal.DEMProcessing(out_tif, layer.source(), "hillshade", zFactor=z_factor)
            display_name = f"{layer_name}_山体阴影"
        elif opt_type in ("slope", "坡度"):
            gdal.DEMProcessing(out_tif, layer.source(), "slope", zFactor=z_factor)
            display_name = f"{layer_name}_坡度分析(度)"
        elif opt_type in ("aspect", "坡向"):
            gdal.DEMProcessing(out_tif, layer.source(), "aspect")
            display_name = f"{layer_name}_坡向分析"
        elif opt_type in ("tri", "崎岖度", "地形崎岖度"):
            gdal.DEMProcessing(out_tif, layer.source(), "TRI")
            display_name = f"{layer_name}_地形崎岖度(TRI)"
        elif opt_type in ("tpi", "地形位置指数"):
            gdal.DEMProcessing(out_tif, layer.source(), "TPI")
            display_name = f"{layer_name}_地形位置指数(TPI)"
        elif opt_type in ("roughness", "粗糙度"):
            gdal.DEMProcessing(out_tif, layer.source(), "roughness")
            display_name = f"{layer_name}_粗糙度"
        else:
            gdal.DEMProcessing(out_tif, layer.source(), "hillshade", zFactor=z_factor)
            display_name = f"{layer_name}_地形分析({analysis_type})"

        lyr = QgsRasterLayer(out_tif, display_name, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"⛰️ **地形分析 [{analysis_type}] 处理完成**：已加载图层 `{display_name}`"
        return "❌ 地形分析处理完成，但图层加载失败。"
    except Exception as e:
        return f"地形分析失败: {e}"


def skill_spatial_filter(layer_name: str, filter_type: str = "sobel", band_idx: int = 1) -> str:
    """【PyQGIS 原生】空间卷积滤波 (sobel 边缘提取, gaussian 平滑, laplacian 锐化)。"""
    try:
        from scipy.ndimage import sobel, gaussian_filter, laplace

        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        arr = ds.GetRasterBand(band_idx).ReadAsArray().astype(np.float32)
        f_type = filter_type.lower()

        if "sobel" in f_type or "边缘" in f_type:
            sx = sobel(arr, axis=0)
            sy = sobel(arr, axis=1)
            filtered = np.hypot(sx, sy)
            display_type = "Sobel边缘提取"
        elif "gaussian" in f_type or "平滑" in f_type:
            filtered = gaussian_filter(arr, sigma=1.5)
            display_type = "高斯平滑"
        elif "laplace" in f_type or "锐化" in f_type:
            filtered = laplace(arr)
            display_type = "拉普拉斯锐化"
        else:
            filtered = arr
            display_type = filter_type

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"filter_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_tif, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(filtered)
        out_ds.FlushCache()
        out_ds = None
        ds = None

        layer_title = f"{layer_name}_{display_type}"
        lyr = QgsRasterLayer(out_tif, layer_title, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"✨ **空间滤波 [{display_type}] 处理完成**：已加载图层 `{layer_title}`"
        return "❌ 滤波图层构建失败。"
    except Exception as e:
        return f"空间滤波失败: {e}"


def skill_area_statistics(layer_name: str) -> str:
    """【PyQGIS 原生】统计分类图层面积与占比（原生支持矢量图斑与栅格像元统计）。"""
    try:
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if not layers:
            raise ValueError(f"找不到图层: '{layer_name}'")
        layer = layers[0]

        # 1. 矢量图层面积统计
        if isinstance(layer, QgsVectorLayer):
            total_area_m2 = sum(f.geometry().area() for f in layer.getFeatures() if f.hasGeometry())
            total_count = layer.featureCount()
            area_mu = total_area_m2 / 666.6667
            area_sqkm = total_area_m2 / 1_000_000
            return (
                f"📊 **矢量图层 `{layer_name}` 面积统计**：\n"
                f"- 要素总数: `{total_count}` 个图斑\n"
                f"- 累计总面积: `{total_area_m2:,.2f} ㎡` (`{area_mu:,.2f} 亩` / `{area_sqkm:.4f} k㎡`)"
            )

        # 2. 栅格分类面积统计
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法读取栅格数据: {layer.source()}"

        gt = ds.GetGeoTransform()
        res_x = abs(gt[1])
        res_y = abs(gt[5])

        # 经纬度投影坐标换算
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        if srs.IsGeographic():
            res_x *= 111320.0
            res_y *= 111320.0

        pixel_area_m2 = res_x * res_y
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray()
        nodata = band.GetNoDataValue()

        valid_mask = (arr != nodata) if nodata is not None else np.ones_like(arr, dtype=bool)
        unique, counts = np.unique(arr[valid_mask], return_counts=True)
        total_pixels = sum(counts)

        report = [f"📊 **栅格图层 `{layer_name}` 像元分类面积统计**："]
        for val, cnt in zip(unique, counts):
            area_m2 = float(cnt * pixel_area_m2)
            area_mu = area_m2 / 666.6667
            pct = (cnt / total_pixels) * 100.0 if total_pixels > 0 else 0.0
            report.append(
                f"- **类别 {int(val)}**: `{int(cnt):,}` 个像元 | `{area_m2:,.2f} ㎡` (`{area_mu:,.2f} 亩`, 占比 `{pct:.1f}%`)"
            )
        ds = None
        return "\n".join(report)
    except Exception as e:
        return f"面积统计失败: {e}"


def skill_vector_smooth(layer_name: str, tolerance: float = 1.0, iterations: int = 2) -> str:
    """【PyQGIS 原生】矢量边界平滑、化简与去锯齿。"""
    try:
        layer = get_layer_by_name(layer_name, "vector")

        # 使用 QGIS 原生平滑算法
        import processing
        params = {
            'INPUT': layer,
            'ITERATIONS': iterations,
            'OFFSET': 0.25,
            'MAX_ANGLE': 180,
            'OUTPUT': 'memory:'
        }
        res = processing.run("native:smoothgeometry", params)
        smoothed_layer = res['OUTPUT']

        smoothed_layer.setName(f"{layer.name()}_平滑")
        QgsProject.instance().addMapLayer(smoothed_layer)
        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        return f"📐 **矢量图层 `{layer_name}` 边界平滑去锯齿完成**：已生成内存图层 `{smoothed_layer.name()}` (迭代次数: {iterations})。"
    except Exception as e:
        return f"矢量平滑失败: {e}"


def skill_kmeans_cluster(layer_name: str, k: int = 5, max_iters: int = 15) -> str:
    """【PyQGIS 原生】多波段 K-Means 无监督聚类。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        bands_data = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
        h, w = bands_data[0].shape
        X = np.stack([b.flatten() for b in bands_data], axis=1)

        # 随机中心点初始化
        np.random.seed(42)
        valid_idx = np.random.choice(X.shape[0], k, replace=False)
        centers = X[valid_idx]

        labels = np.zeros(X.shape[0], dtype=np.int32)
        for _ in range(max_iters):
            dists = np.linalg.norm(X[:, np.newaxis] - centers, axis=2)
            new_labels = np.argmin(dists, axis=1)
            if np.all(labels == new_labels):
                break
            labels = new_labels
            for c in range(k):
                mask = (labels == c)
                if np.any(mask):
                    centers[c] = np.mean(X[mask], axis=0)

        clustered = labels.reshape((h, w)).astype(np.uint8)

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"kmeans_k{k}_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_tif, w, h, 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())
        out_ds.GetRasterBand(1).WriteArray(clustered)
        out_ds.FlushCache()
        out_ds = None
        ds = None

        out_name = f"{layer_name}_KMeans聚类(K={k})"
        lyr = QgsRasterLayer(out_tif, out_name, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"🧩 **K-Means 智能聚类完成**：已生成 {k} 个地物聚类图层 `{out_name}`"
        return "❌ 聚类结果加载失败。"
    except Exception as e:
        return f"K-Means 失败: {e}"


def skill_raster_diff(layer_t1: str, layer_t2: str, threshold: float = 30.0, polygonize: bool = True) -> str:
    """【PyQGIS 原生】双期影像像元级绝对差分变化检测。"""
    try:
        l1 = get_layer_by_name(layer_t1, "raster")
        l2 = get_layer_by_name(layer_t2, "raster")

        d1 = gdal.Open(l1.source())
        d2 = gdal.Open(l2.source())
        if not d1 or not d2:
            return "❌ 无法打开双期影像文件。"

        a1 = d1.GetRasterBand(1).ReadAsArray().astype(np.float32)
        a2 = d2.GetRasterBand(1).ReadAsArray().astype(np.float32)

        if a1.shape != a2.shape:
            return f"❌ 双期影像尺寸不一致：T1 为 {a1.shape}，T2 为 {a2.shape}，无法直接差分。"

        diff = np.abs(a1 - a2)
        mask = (diff >= threshold).astype(np.uint8)

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"diff_mask_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(out_tif, a1.shape[1], a1.shape[0], 1, gdal.GDT_Byte)
        out_ds.SetGeoTransform(d1.GetGeoTransform())
        out_ds.SetProjection(d1.GetProjection())
        band = out_ds.GetRasterBand(1)
        band.WriteArray(mask)
        band.SetNoDataValue(0)
        out_ds.FlushCache()
        out_ds = None
        d1 = None
        d2 = None

        diff_layer_name = f"双期差分变化掩膜(阈值{threshold})"
        lyr = QgsRasterLayer(out_tif, diff_layer_name, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)

        poly_msg = ""
        if polygonize:
            poly_msg = "\n" + skill_raster_polygonize(diff_layer_name, sieve_size=4)

        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        return f"🔄 **双期影像差分变化检测完成**：已生成变化掩膜图层 `{diff_layer_name}`{poly_msg}"
    except Exception as e:
        return f"差分检测失败: {e}"


def skill_image_enhance(layer_name: str, r: int = 4, g: int = 3, b: int = 2) -> str:
    """【PyQGIS 原生】多波段假彩色合成与 2% 线性拉伸增强。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        ds = gdal.Open(layer.source())
        if ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        max_b = ds.RasterCount
        if max(r, g, b) > max_b:
            return f"❌ 波段索引越界：栅格仅包含 {max_b} 个波段，无法以 ({r},{g},{b}) 组合合成。"

        bands = [ds.GetRasterBand(b_idx).ReadAsArray().astype(np.float32) for b_idx in (r, g, b)]
        enhanced_bands = []

        for arr in bands:
            p2, p98 = np.percentile(arr, (2, 98))
            if p98 > p2:
                arr = (arr - p2) / (p98 - p2) * 255.0
            arr = np.clip(arr, 0, 255)
            enhanced_bands.append(arr.astype(np.uint8))

        time_str = datetime.now().strftime("%H%M%S")
        out_tif = os.path.join(tempfile.gettempdir(), f"enhance_{r}_{g}_{b}_{time_str}.tif")

        driver = gdal.GetDriverByName("GTiff")
        h, w = enhanced_bands[0].shape
        out_ds = driver.Create(out_tif, w, h, 3, gdal.GDT_Byte)
        out_ds.SetGeoTransform(ds.GetGeoTransform())
        out_ds.SetProjection(ds.GetProjection())

        for i in range(3):
            out_ds.GetRasterBand(i + 1).WriteArray(enhanced_bands[i])

        out_ds.FlushCache()
        out_ds = None
        ds = None

        out_title = f"{layer_name}_彩色增强({r},{g},{b})"
        lyr = QgsRasterLayer(out_tif, out_title, "gdal")
        if lyr.isValid():
            QgsProject.instance().addMapLayer(lyr)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"🎨 **多波段假彩色合成与画质增强完成**：已加载图层 `{out_title}`"
        return "❌ 增强图层生成失败。"
    except Exception as e:
        return f"画质增强失败: {e}"


def skill_raster_polygonize(layer_name: str, sieve_size: int = 4) -> str:
    """【PyQGIS 原生】二值/分类栅格矢量化为 Polygon 面要素，自动过滤碎斑。"""
    try:
        layer = get_layer_by_name(layer_name, "raster")
        src_ds = gdal.Open(layer.source())
        if src_ds is None:
            return f"❌ 无法打开栅格: {layer.source()}"

        src_band = src_ds.GetRasterBand(1)
        time_str = datetime.now().strftime("%H%M%S")
        out_shp = os.path.join(tempfile.gettempdir(), f"poly_{time_str}.shp")

        srs = osr.SpatialReference(wkt=src_ds.GetProjection())
        drv = ogr.GetDriverByName("ESRI Shapefile")
        if os.path.exists(out_shp):
            drv.DeleteDataSource(out_shp)

        dst_ds = drv.CreateDataSource(out_shp)
        dst_layer = dst_ds.CreateLayer("polygonized", srs=srs, geom_type=ogr.wkbPolygon)

        fd = ogr.FieldDefn("DN", ogr.OFTInteger)
        dst_layer.CreateField(fd)

        # GDAL 原生矢量化
        gdal.Polygonize(src_band, None, dst_layer, 0, [], callback=None)

        dst_ds.FlushCache()
        dst_ds = None
        src_ds = None

        out_vec_name = f"{layer_name}_矢量化图斑"
        vlayer = QgsVectorLayer(out_shp, out_vec_name, "ogr")
        if vlayer.isValid():
            # 过滤背景 (DN > 0)
            vlayer.setSubsetString("DN > 0")
            QgsProject.instance().addMapLayer(vlayer)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"📦 **栅格 `{layer_name}` 已成功转换为矢量多边形**：已加载图层 `{out_vec_name}` ({vlayer.featureCount()} 个图斑)。"
        return "❌ 矢量化图层加载失败。"
    except Exception as e:
        return f"矢量化失败: {e}"


# ===========================================================================
# 4. 云端 AI 深度解译任务调度
# ===========================================================================

def _sanitize_ai_task_extent(layer: QgsRasterLayer, extent=None, extent_crs=None) -> Tuple[QgsRectangle, QgsCoordinateReferenceSystem]:
    """提取解译区域范围。"""
    canvas = iface.mapCanvas() if iface else None

    if extent is None:
        extent = canvas.extent() if canvas else layer.extent()
        extent_crs = canvas.mapSettings().destinationCrs() if canvas else layer.crs()
    if extent_crs is None:
        extent_crs = layer.crs()

    return extent, extent_crs


def skill_ai_extract_feature(
    layer_name: str, feature_type: str, server_url: str, token: str,
    machine_id: str, extent=None, extent_crs=None,
):
    """【卫星影像标准地物模型】建筑/水体/道路/林地/草地/耕地/施工。"""
    from ..tasks.interpret_task import InterpretTask

    layer = get_layer_by_name(layer_name, "raster")
    profile = _inspect_raster_profile(layer)

    if profile["resolution_m"] < 0.35:
        logger.warning("无人机厘米级影像建议改用 SAM3。")

    target_class_id = find_class_ids_by_keywords([feature_type], fallback_id="5")
    real_model_key = get_model_key_by_mode("landuse", fallback_key="LANDUSE")

    task_extent, task_extent_crs = _sanitize_ai_task_extent(layer, extent, extent_crs)

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

    layer = get_layer_by_name(layer_name, "raster")
    real_model_key = get_model_key_by_mode("sam3", fallback_key="SAM3_MODEL")

    task_extent, task_extent_crs = _sanitize_ai_task_extent(layer, extent, extent_crs)

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

    l1 = get_layer_by_name(layer_t1, "raster")
    l2 = get_layer_by_name(layer_t2, "raster")
    real_model_key = get_model_key_by_mode("change_detection", fallback_key="CHANGE_DETECTION")

    task_extent, task_extent_crs = _sanitize_ai_task_extent(l1, extent, extent_crs)

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
    """执行原生 QGIS 算法并自动加载结果。"""
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
        "math": math,
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


# ===========================================================================
# 7. OpenStreetMap 真实矢量数据接口 (Overpass API)
# ===========================================================================

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
]


def skill_fetch_osm_vector_data(
        place_name: str = "当前视口",
        feature_type: str = "all",
        custom_tag: str = ""
) -> str:
    """
    通过 Overpass API 获取 OpenStreetMap 真实矢量 JSON 数据，并在 QGIS 中自动生成矢量图层。
    """
    try:
        bbox, located_msg = _get_target_bbox(place_name)
        min_lon, min_lat, max_lon, max_lat = bbox

        tag_filter = ""
        if custom_tag:
            tag_filter = f"[{custom_tag}]"
        elif feature_type == "building":
            tag_filter = '["building"]'
        elif feature_type == "highway":
            tag_filter = '["highway"]'
        elif feature_type == "water":
            tag_filter = '["natural"~"water|wetland"]'
        elif feature_type == "amenity":
            tag_filter = '["amenity"]'
        elif feature_type == "landuse":
            tag_filter = '["landuse"]'

        overpass_query = f"""
        [out:json][timeout:25];
        (
          node{tag_filter}({min_lat},{min_lon},{max_lat},{max_lon});
          way{tag_filter}({min_lat},{min_lon},{max_lat},{max_lon});
          relation{tag_filter}({min_lat},{min_lon},{max_lat},{max_lon});
        );
        out body;
        >;
        out skel qt;
        """

        headers = {"User-Agent": "GeoMind-QGIS-Plugin/1.0"}
        data = None
        for server in OVERPASS_SERVERS:
            try:
                resp = requests.post(server, data={"data": overpass_query}, headers=headers, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    break
            except Exception:
                continue

        if not data or "elements" not in data:
            return f"{located_msg}❌ 获取 OSM 矢量 JSON 数据失败：Overpass 接口连接超时，请缩小视口后重试。"

        elements = data.get("elements", [])
        if not elements:
            return f"{located_msg}ℹ️ 当前区域内未检索到符合条件的 OSM 矢量要素。"

        nodes_dict = {}
        for elem in elements:
            if elem.get("type") == "node" and "lon" in elem and "lat" in elem:
                nodes_dict[elem["id"]] = QgsPointXY(elem["lon"], elem["lat"])

        point_features, line_features, poly_features = [], [], []

        for elem in elements:
            tags = elem.get("tags", {})
            if not tags:
                continue

            elem_id = str(elem.get("id"))
            name = tags.get("name") or tags.get("name:en") or tags.get("name:zh") or "未命名"
            elem_type = tags.get("building") or tags.get("highway") or tags.get("amenity") or tags.get(
                "natural") or tags.get("landuse") or "osm_feature"
            tags_json_str = json.dumps(tags, ensure_ascii=False)

            if elem.get("type") == "node":
                if elem["id"] in nodes_dict:
                    feat = QgsFeature()
                    feat.setGeometry(QgsGeometry.fromPointXY(nodes_dict[elem["id"]]))
                    feat.setAttributes([elem_id, name, elem_type, tags_json_str])
                    point_features.append(feat)

            elif elem.get("type") == "way":
                node_ids = elem.get("nodes", [])
                pts = [nodes_dict[nid] for nid in node_ids if nid in nodes_dict]
                if len(pts) < 2:
                    continue

                is_polygon = (len(pts) >= 4 and pts[0] == pts[-1] and (
                        "building" in tags or "landuse" in tags or "natural" in tags or "area" in tags
                ))

                if is_polygon:
                    feat = QgsFeature()
                    feat.setGeometry(QgsGeometry.fromPolygonXY([pts]))
                    feat.setAttributes([elem_id, name, elem_type, tags_json_str])
                    poly_features.append(feat)
                else:
                    feat = QgsFeature()
                    feat.setGeometry(QgsGeometry.fromPolylineXY(pts))
                    feat.setAttributes([elem_id, name, elem_type, tags_json_str])
                    line_features.append(feat)

        time_tag = datetime.now().strftime("%H%M%S")
        loaded_layers = []

        def create_layer(geom_type, name, features):
            vlayer = QgsVectorLayer(f"{geom_type}?crs=EPSG:4326", name, "memory")
            prov = vlayer.dataProvider()
            prov.addAttributes([
                QgsField("osm_id", QVariant.String),
                QgsField("name", QVariant.String),
                QgsField("type", QVariant.String),
                QgsField("tags", QVariant.String),
            ])
            vlayer.updateFields()
            prov.addFeatures(features)
            vlayer.updateExtents()
            QgsProject.instance().addMapLayer(vlayer)
            return vlayer.name()

        if poly_features:
            lyr = create_layer("Polygon", f"OSM_面要素_{feature_type}_{time_tag}", poly_features)
            loaded_layers.append(f"`{lyr}` ({len(poly_features)} 个面)")
        if line_features:
            lyr = create_layer("LineString", f"OSM_线要素_{feature_type}_{time_tag}", line_features)
            loaded_layers.append(f"`{lyr}` ({len(line_features)} 条线)")
        if point_features:
            lyr = create_layer("Point", f"OSM_点要素_{feature_type}_{time_tag}", point_features)
            loaded_layers.append(f"`{lyr}` ({len(point_features)} 个点)")

        if iface and iface.mapCanvas():
            iface.mapCanvas().refresh()

        if not loaded_layers:
            return f"{located_msg}⚠️ 获取到了 {len(elements)} 个 OSM 拓扑节点，但未构建出有效几何要素。"

        return (
                f"{located_msg}🎉 **已成功获取 OpenStreetMap 真实 JSON 矢量数据并加载至工程**：\n"
                + "\n".join([f"- {l}" for l in loaded_layers])
                + "\n💡 *图层属性表中包含原始 OSM 标签属性 (name, type, tags)，可直接参与空间分析或编辑。*"
        )

    except Exception as e:
        return f"获取 OSM 矢量数据异常: {e}"


# ===========================================================================
# 8. 全球多源开放数据接入引擎 (Natural Earth, Landsat, WorldCover, WorldPop 等)
# ===========================================================================

def skill_fetch_natural_earth(feature_type: str = "countries") -> str:
    """拉取 Natural Earth 1:10m 全球基础地理矢量图层。"""
    try:
        url_map = {
            "countries": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson",
            "coastline": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_coastline.geojson",
            "rivers": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_rivers_lake_centerlines.geojson",
            "places": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_populated_places.geojson",
        }
        target_url = url_map.get(feature_type, url_map["countries"])
        layer_name = f"NaturalEarth_10m_{feature_type.title()}"

        vlayer = QgsVectorLayer(target_url, layer_name, "ogr")
        if vlayer.isValid():
            QgsProject.instance().addMapLayer(vlayer)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return f"🌍 **Natural Earth 1:10m 基础地理矢量图层已加载**：`{layer_name}` ({vlayer.featureCount()} 个要素)"
        return "❌ Natural Earth 图层加载失败，请检查网络连接。"
    except Exception as e:
        return f"获取 Natural Earth 异常: {e}"


def skill_fetch_landsat_imagery(
    place_name: str = "当前视口",
    days_back: int = 30,
    max_cloud: int = 20
) -> str:
    """检索并流式加载 Landsat 8/9 C2-L2 30米多光谱卫星影像。"""
    try:
        bbox, located_msg = _get_target_bbox(place_name)
        today = datetime.now()
        start_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        payload = {
            "collections": ["landsat-c2-l2"],
            "bbox": bbox,
            "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": max_cloud}, "platform": {"in": ["landsat-8", "landsat-9"]}},
            "limit": 3,
            "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}]
        }
        resp = requests.post(STAC_AWS_URL, json=payload, timeout=15)
        features = resp.json().get("features", [])
        if not features:
            return f"{located_msg}在近 {days_back} 天内未检索到云量 < {max_cloud}% 的 Landsat 8/9 影像，建议放宽检索时间。"

        item = features[0]
        item_id = item.get("id", "")
        props = item.get("properties", {})
        acq_time = props.get("datetime", "")[:10]
        cloud = props.get("eo:cloud_cover", 0.0)
        assets = item.get("assets", {})

        band_keys = ["red", "green", "blue", "nir08"]
        if all(k in assets for k in band_keys):
            band_urls = [f"/vsicurl/{assets[k]['href']}" for k in band_keys]
            raw_vrt = os.path.join(tempfile.gettempdir(), f"landsat_{item_id}_raw.vrt")
            gdal.BuildVRT(raw_vrt, band_urls, options=gdal.BuildVRTOptions(separate=True))

            warp_options = gdal.WarpOptions(
                format="VRT",
                outputBounds=[bbox[0], bbox[1], bbox[2], bbox[3]],
                outputBoundsSRS="EPSG:4326",
                resampleAlg="bilinear"
            )
            clipped_vrt = os.path.join(tempfile.gettempdir(), f"landsat_{item_id}_screen.vrt")
            gdal.Warp(clipped_vrt, raw_vrt, options=warp_options)

            layer_name = f"Landsat8/9_{acq_time}_4波段(视口裁剪)_云量{cloud:.1f}%"
            layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                return f"{located_msg}🛰️ **已成功流式加载 30米 Landsat 8/9 卫星影像**：`{layer_name}`"

        return f"{located_msg}Landsat 影像波段解析失败。"
    except Exception as e:
        return f"获取 Landsat 影像异常: {e}"


STAC_MPC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
MPC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"


def _mpc_sign_href(href: str) -> str:
    """为 Planetary Computer 上的受保护 blob 资产签发临时可匿名访问的 SAS URL。"""
    try:
        resp = requests.get(MPC_SIGN_URL, params={"href": href}, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("href", href)
    except Exception as e:
        logger.warning(f"MPC SAS 签名失败,回退为原始 href: {e}")
    return href


def skill_fetch_sentinel1_sar(
    place_name: str = "当前视口",
    date_start: str = None,
    date_end: str = None,
    days_back: int = 14,
    polarization: str = "vv"
) -> str:
    """
    【在线遥感数据拉取】检索并流式加载 Sentinel-1 GRD 哨兵微波雷达影像 (SAR)。
    """
    try:
        bbox, located_msg = _get_target_bbox(place_name)

        today = datetime.now()
        if not date_end:
            date_end = today.strftime("%Y-%m-%d")
        if not date_start:
            date_start = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")

        pol = (polarization or "vv").lower()
        if pol not in ("vv", "vh", "hh", "hv"):
            pol = "vv"

        payload = {
            "collections": ["sentinel-1-grd"],
            "bbox": bbox,
            "datetime": f"{date_start}T00:00:00Z/{date_end}T23:59:59Z",
            "limit": 5,
            "sortby": [{"field": "datetime", "direction": "desc"}]
        }
        resp = requests.post(STAC_MPC_URL, json=payload, timeout=15)
        if resp.status_code != 200:
            return f"{located_msg}Sentinel-1 SAR 检索服务异常 (HTTP {resp.status_code})"

        features = resp.json().get("features", [])
        if not features:
            return (f"{located_msg}在 {date_start} 至 {date_end} 期间未检索到该区域的 "
                     f"Sentinel-1 SAR 影像,请放宽日期范围重试。")

        item = None
        asset_key = None
        for feat in features:
            assets = feat.get("assets", {})
            if pol in assets:
                item, asset_key = feat, pol
                break
        if item is None:
            for feat in features:
                assets = feat.get("assets", {})
                for k in ("vv", "vh", "hh", "hv"):
                    if k in assets:
                        item, asset_key = feat, k
                        break
                if item:
                    break

        if item is None:
            return f"{located_msg}检索到 {len(features)} 景 Sentinel-1 影像,但均未包含可用的极化波段数据。"

        props = item.get("properties", {})
        acq_time = props.get("datetime", "")[:10]
        orbit = props.get("sat:orbit_state", "")
        item_id = item.get("id", "")
        raw_href = item["assets"][asset_key]["href"]
        signed_href = _mpc_sign_href(raw_href)

        warp_options = gdal.WarpOptions(
            format="VRT",
            outputBounds=[bbox[0], bbox[1], bbox[2], bbox[3]],
            outputBoundsSRS="EPSG:4326",
            srcSRS="EPSG:4326",
            dstSRS="EPSG:4326",
            tps=True,
            resampleAlg="bilinear"
        )
        clipped_vrt = os.path.join(tempfile.gettempdir(), f"s1_{item_id}_{asset_key}_screen.vrt")
        gdal.Warp(clipped_vrt, f"/vsicurl/{signed_href}", options=warp_options)

        layer_name = f"Sentinel1_SAR_{acq_time}_{asset_key.upper()}极化_{orbit or '未知轨道'}"
        layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            if iface and iface.mapCanvas():
                iface.mapCanvas().refresh()
            return (f"{located_msg}📡 **已成功流式加载 Sentinel-1 SAR 雷达影像**:`{layer_name}`\n"
                    f"(不受云层影响,像元值为后向散射振幅)")

        return f"{located_msg}Sentinel-1 SAR 影像加载失败:栅格图层无效。"
    except Exception as e:
        logger.error(f"Sentinel-1 SAR fetch failed: {e}")
        return f"获取 Sentinel-1 SAR 影像异常: {e}"


def skill_fetch_worldcover_lulc(place_name: str = "当前视口") -> str:
    """检索并流式挂载 ESA WorldCover 10米全球土地利用覆盖分类图。"""
    try:
        bbox, located_msg = _get_target_bbox(place_name)
        worldcover_vrt_url = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v100/2020/esa_worldcover_2020_10m.vrt"
        vsi_url = f"/vsicurl/{worldcover_vrt_url}"

        time_str = datetime.now().strftime("%H%M%S")
        warp_opts = gdal.WarpOptions(
            format="VRT",
            outputBounds=[bbox[0], bbox[1], bbox[2], bbox[3]],
            outputBoundsSRS="EPSG:4326"
        )
        clipped_vrt = os.path.join(tempfile.gettempdir(), f"worldcover_{time_str}.vrt")
        gdal.Warp(clipped_vrt, vsi_url, options=warp_opts)

        layer_name = f"ESA_WorldCover_10m_土地覆盖_{time_str}"
        layer = QgsRasterLayer(clipped_vrt, layer_name, "gdal")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return (
                f"{located_msg}🌳 **已成功加载 ESA WorldCover 10米全球土地利用分类图**：`{layer_name}`\n"
                f"🏷️ *分类体系包含：林地(10)、灌木(20)、草地(30)、农田(40)、建筑区(50)、裸地(60)、水体(80)、湿地(90)等。*"
            )
        return f"{located_msg}ESA WorldCover 图层构建失败。"
    except Exception as e:
        return f"获取 WorldCover 异常: {e}"


def skill_fetch_worldpop_density(place_name: str = "当前视口", year: int = 2020) -> str:
    """加载 WorldPop 全球 100米 人口密度与空间分布栅格图层。"""
    try:
        _, located_msg = _get_target_bbox(place_name)
        wms_url = (
            "crs=EPSG:4326&dpiMode=7&format=image/png&layers=wp:pop_density_"
            f"{year}&styles=&url=https://hub.worldpop.org/geoserver/wms"
        )
        layer_name = f"WorldPop_{year}_全球人口密度(100m)"
        layer = QgsRasterLayer(wms_url, layer_name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return f"{located_msg}👥 **已成功加载 WorldPop 全球 100米 人口密度栅格图层**：`{layer_name}`"
        return f"{located_msg}WorldPop 人口数据服务连接异常。"
    except Exception as e:
        return f"获取 WorldPop 异常: {e}"


def skill_fetch_nighttime_lights(place_name: str = "当前视口") -> str:
    """流式加载 NOAA VIIRS DNB 500米 全球夜间灯光辐射影像。"""
    try:
        _, located_msg = _get_target_bbox(place_name)
        wmts_url = (
            "crs=EPSG:4326&dpiMode=7&format=image/png&layers=VIIRS_SNPP_DayNightBand_ENCC"
            "&styles=default&url=https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/wmts.cgi"
        )
        layer_name = "VIIRS_全球夜间灯光"
        layer = QgsRasterLayer(wmts_url, layer_name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return f"{located_msg}🌃 **已成功加载 VIIRS 全球夜间灯光影像**：`{layer_name}`\n*(可用于分析城市化边界、经济活力与电力分布)*"
        return f"{located_msg}夜间灯光服务连接失败。"
    except Exception as e:
        return f"获取夜间灯光数据异常: {e}"


def skill_fetch_hydrology_data(place_name: str = "当前视口") -> str:
    """加载 HydroSHEDS 全球水文流域与河网等级矢量数据。"""
    try:
        _, located_msg = _get_target_bbox(place_name)
        wms_url = (
            "crs=EPSG:4326&dpiMode=7&format=image/png&layers=hydrosheds:hydro_rivers"
            "&styles=&url=https://hydrosheds.org/geoserver/wms"
        )
        layer_name = "HydroSHEDS_全球河网水系"
        layer = QgsRasterLayer(wms_url, layer_name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            return f"{located_msg}💧 **已成功挂载 HydroSHEDS 全球河网与流域水文图层**：`{layer_name}`"
        return f"{located_msg}HydroSHEDS 水文服务连接异常。"
    except Exception as e:
        return f"获取水文数据异常: {e}"


def skill_fetch_era5_climate(place_name: str = "当前视口", days_back: int = 7) -> str:
    """查询指定地点近期的 ERA5 气象历史再分析数据（气温、降雨量、风速、气压）。"""
    try:
        bbox, _ = _get_target_bbox(place_name)
        center_lon = (bbox[0] + bbox[2]) / 2
        center_lat = (bbox[1] + bbox[3]) / 2

        today = datetime.now()
        start_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
        end_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        url = "https://archive-api.open-meteo.com/v1/era5"
        params = {
            "latitude": round(center_lat, 3),
            "longitude": round(center_lon, 3),
            "start_date": start_date,
            "end_date": end_date,
            "hourly": "temperature_2m,precipitation,windspeed_10m,surface_pressure",
            "timezone": "auto"
        }
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            hourly = data.get("hourly", {})
            temps = hourly.get("temperature_2m", [])
            precips = hourly.get("precipitation", [])
            winds = hourly.get("windspeed_10m", [])

            avg_temp = sum(temps) / len(temps) if temps else 0
            total_rain = sum(precips)
            max_wind = max(winds) if winds else 0

            return (
                f"⛅ **ERA5 气象重分析结果** ({place_name} [{center_lon:.2f}E, {center_lat:.2f}N]，近 {days_back} 天):\n"
                f"- **平均气温**: `{avg_temp:.1f} °C` (最高 `{max(temps):.1f} °C` / 最低 `{min(temps):.1f} °C`)\n"
                f"- **累计降水量**: `{total_rain:.1f} mm`\n"
                f"- **最大阵风风速**: `{max_wind:.1f} km/h`\n"
                f"💡 *数据源: ECMWF ERA5 全球气候再分析数据集。*"
            )
        return "ERA5 气象接口请求超时。"
    except Exception as e:
        return f"获取 ERA5 气象数据异常: {e}"


# ===========================================================================
# 9. 实时联网搜索与网页内容提取 (Bing / 百度双通道)
# ===========================================================================

def skill_web_search(query: str, max_results: int = 5) -> str:
    """通用实时联网搜索工具（国内直连免Key免费版：Bing中国 + 百度双通道）。"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    }

    results = []

    # 通道 1：Bing 中国
    try:
        encoded_query = urllib.parse.quote(query)
        bing_url = f"https://cn.bing.com/search?q={encoded_query}&ensearch=0"

        resp = requests.get(bing_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            raw_html = resp.text
            blocks = re.findall(r'<li class="b_algo"(.*?)</li>', raw_html, re.DOTALL)

            for b in blocks[:max_results]:
                t_m = re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', b, re.DOTALL)
                s_m = re.search(r'<div class="b_caption"><p[^>]*>(.*?)</p>', b, re.DOTALL) or re.search(
                    r'<p[^>]*>(.*?)</p>', b, re.DOTALL)

                if t_m:
                    link = t_m.group(1)
                    title = re.sub(r'<[^>]+>', '', t_m.group(2)).strip()
                    snippet = re.sub(r'<[^>]+>', '', s_m.group(1)).strip() if s_m else "无摘要"

                    title = unescape(title)
                    snippet = unescape(snippet)

                    if title and link.startswith("http"):
                        results.append(f"📌 **[{title}]({link})**\n   {snippet}")

            if results:
                return f"🔍 **Bing 搜索结果 (`{query}`)**：\n\n" + "\n\n".join(results)
    except Exception as e:
        logger.warning(f"Bing search failed, falling back to Baidu: {e}")

    # 通道 2：百度搜索
    try:
        baidu_url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}"
        resp = requests.get(baidu_url, headers=headers, timeout=8)
        if resp.status_code == 200:
            raw_html = resp.text
            blocks = re.findall(r'<div class="[a-z0-9-_]*\s*c-container[^"]*"(.*?)</div>\s*</div>', raw_html, re.DOTALL)

            for b in blocks[:max_results]:
                t_m = re.search(r'<h3[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h3>', b, re.DOTALL)
                s_m = re.search(r'<span class="content-right_[^"]*"[^>]*>(.*?)</span>', b, re.DOTALL) or re.search(
                    r'<div class="c-abstract"[^>]*>(.*?)</div>', b, re.DOTALL)

                if t_m:
                    link = t_m.group(1)
                    title = re.sub(r'<[^>]+>', '', t_m.group(2)).strip()
                    snippet = re.sub(r'<[^>]+>', '', s_m.group(1)).strip() if s_m else "无摘要"

                    title = unescape(title)
                    snippet = unescape(snippet)

                    if title:
                        results.append(f"📌 **[{title}]({link})**\n   {snippet}")

            if results:
                return f"🔍 **百度搜索结果 (`{query}`)**：\n\n" + "\n\n".join(results)
    except Exception as e_baidu:
        logger.warning(f"Baidu search fallback failed: {e_baidu}")

    return "❌ 联网搜索失败：当前网络未能连接到 Bing/百度搜索服务，请稍后重试。"


def skill_fetch_webpage_content(url: str, max_chars: int = 2500) -> str:
    """抓取并提取指定网页的正文文本内容（自动清洗 HTML 标签与冗余脚本）。"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=12)
        resp.encoding = resp.apparent_encoding or "utf-8"

        if resp.status_code != 200:
            return f"❌ 抓取网页失败，HTTP 状态码: {resp.status_code}"

        html = resp.text
        html = re.sub(r'<(script|style|head|noscript)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        text = unescape(text)

        if not text:
            return "⚠️ 网页抓取成功，但未提取到有效文本内容（可能是纯 JS 渲染页面）。"

        preview = text[:max_chars]
        truncated_msg = f"\n\n*(正文已截断，前 {max_chars} 字符)*" if len(text) > max_chars else ""
        return f"📄 **网页正文提取自**：`{url}`\n\n{preview}{truncated_msg}"

    except Exception as e:
        return f"抓取网页异常: {e}"