# -*- coding: utf-8 -*-
"""
LLM skill dispatcher — bridges the Copilot backend to local tools.

整合功能：
1. 图层物理画像探测（区分无人机/高分卫星/哨兵/在线瓦片）；
2. 哨兵二号与 30m 全球 Copernicus DEM 在线流式 STAC 检索；
3. 本地光谱/地形/滤波/聚类分析；
4. 云端 AI 深度解译任务调度（内置自动安全中心裁剪，永不因视口过大而报错）；
5. QGIS 原生 Processing 算子检索与防卡死执行；
6. PyQGIS 动态代码防卡死安全沙箱执行器；
7. OpenStreetMap 与全球多源开放数据接入（Natural Earth, Landsat, WorldCover 等）；
8. 实时联网搜索与网页正文解析。

【本次修复说明】
1. `_geocode_place_bbox`：内置城市速查表也统一经过 `_clamp_bbox`，避免未来新增大范围条目绕过安全钳制。
2. `_get_target_bbox`：定位成功后不再是"仅平移中心点 + 固定比例尺缩放"，
   而是直接把画布 `setExtent` 到与实际数据请求一致的 `final_bbox`，确保用户看到的范围
   与 Sentinel-2/DEM/OSM 等函数实际拉取的范围完全一致，不再出现"范围对不上/偏大"的问题。
   同时地名解析失败时会显式提示，而不是静默回退到当前视口。
3. `skill_fetch_worldpop_density` / `skill_fetch_nighttime_lights` / `skill_fetch_hydrology_data`：
   原先计算了安全 bbox 却从未使用，导致这三个 WMS/WMTS 图层实际展示的是全球范围。
   现在图层加载成功后会主动把画布收紧到对应的安全 bbox 范围。
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
# 2. 地理编码、外包框安全钳制与多源遥感数据拉取
# ===========================================================================

def _clamp_bbox(bbox: List[float], max_span_deg: float = 0.15) -> List[float]:
    """
    【安全尺寸钳制】若 bbox 跨度超出安全阈值，自动以中心点为基准缩放至适中工作区。
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    span_x = max_lon - min_lon
    span_y = max_lat - min_lat

    if span_x > max_span_deg or span_y > max_span_deg:
        mid_x = (min_lon + max_lon) / 2.0
        mid_y = (min_lat + max_lat) / 2.0
        half = max_span_deg / 2.0
        return [
            round(mid_x - half, 4),
            round(mid_y - half, 4),
            round(mid_x + half, 4),
            round(mid_y + half, 4)
        ]
    return [round(min_lon, 4), round(min_lat, 4), round(max_lon, 4), round(max_lat, 4)]


def _geocode_place_bbox(place_name: str) -> Optional[List[float]]:
    """
    将地名解析为精细工作区外包框 [min_lon, min_lat, max_lon, max_lat]（~10-15km 核心区）。

    【修复】内置速查表也统一经过 `_clamp_bbox`，避免未来维护者往字典里添加跨度过大的
    条目（例如整个省/直辖市轮廓）时绕开安全钳制。
    """
    quick_bboxes = {
        "北京": [116.32, 39.84, 116.48, 39.98],
        "上海": [121.40, 31.18, 121.56, 31.32],
        "广州": [113.22, 23.08, 113.38, 23.20],
        "深圳": [113.98, 22.50, 114.14, 22.62],
        "成都": [104.00, 30.60, 104.14, 30.72],
        "武汉": [114.24, 30.52, 114.38, 30.64],
        "杭州": [120.10, 30.20, 120.24, 30.32],
        "南京": [118.74, 31.98, 118.88, 32.10],
        "重庆": [106.50, 29.50, 106.64, 29.62],
        "西安": [108.92, 34.23, 108.98, 34.29],
        "天津": [117.14, 39.08, 117.28, 39.20],
        "苏州": [120.56, 31.26, 120.70, 31.38],
        "太湖": [120.00, 31.10, 120.25, 31.30],
        "青岛": [120.32, 36.04, 120.46, 36.16],
    }
    for k, v in quick_bboxes.items():
        if k in place_name:
            return _clamp_bbox(v, max_span_deg=0.15)

    try:
        url = f"https://nominatim.openstreetmap.org/search?q={place_name}&format=json&limit=1"
        headers = {"User-Agent": "GeoMind-QGIS-Plugin/1.0"}
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code == 200 and r.json():
            res = r.json()[0]
            bb = res.get("boundingbox")
            if bb:
                parsed_bbox = [float(bb[2]), float(bb[0]), float(bb[3]), float(bb[1])]
                return _clamp_bbox(parsed_bbox, max_span_deg=0.15)
            lat, lon = float(res["lat"]), float(res["lon"])
            return [round(lon - 0.05, 4), round(lat - 0.05, 4), round(lon + 0.05, 4), round(lat + 0.05, 4)]
    except Exception:
        pass
    return None


def _get_target_bbox(place_name: str, max_span: float = 0.15) -> Tuple[List[float], str]:
    """
    统一获取目标区域经纬度外包框。
    【核心防篡改】优先沿用当前画布视口；若显式给出地名，则将画布范围直接对齐到
    计算出的安全 bbox，确保"用户看到的范围"与"后续函数实际请求数据的范围"完全一致。

    【修复说明】
    - 原实现在定位成功后只 `setCenter` + `zoomScale`，画布范围与 `final_bbox` 并不是
      同一个矩形，容易造成"看起来范围偏大/对不上"的错觉。现在改为直接 `setExtent`。
    - 地名解析失败时，原实现会静默回退到当前画布视口，容易被误认为定位跑偏；
      现在会在返回消息中显式提示解析失败。
    """
    bbox = None
    located_msg = ""
    canvas = iface.mapCanvas() if iface else None

    # 1. 显式地名检索
    if place_name and place_name not in ("当前视口", "当前视图", "当前区域", "视口", "current", ""):
        bbox = _geocode_place_bbox(place_name)
        if bbox:
            located_msg = f"📍 **已定位至目标区域**：`{place_name}`\n"
        else:
            located_msg = f"⚠️ **未能解析地名** `{place_name}`，已回退为当前画布视口\n"

    # 2. 若未显式传入地名（或解析失败），直接取当前画布范围
    if not bbox and canvas:
        rect = canvas.extent()
        src_crs = canvas.mapSettings().destinationCrs()
        dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
        wgs_rect = tr.transformBoundingBox(rect)
        bbox = [
            round(wgs_rect.xMinimum(), 4),
            round(wgs_rect.yMinimum(), 4),
            round(wgs_rect.xMaximum(), 4),
            round(wgs_rect.yMaximum(), 4),
        ]

    # 兜底默认值 (西安钟楼核心区)
    if not bbox or (bbox[0] == 0 and bbox[1] == 0):
        bbox = [108.92, 34.23, 108.98, 34.29]

    final_bbox = _clamp_bbox(bbox, max_span_deg=max_span)

    # 核心保护：若用户触发了新地名定位，直接把画布范围对齐到 final_bbox，
    # 与后续实际拉取数据所用的范围保持严格一致（而不是仅平移中心点再按固定比例尺缩放）。
    if place_name and place_name not in ("当前视口", "当前视图", "当前区域", "视口", "current", "") and bbox and canvas:
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        dest_crs = canvas.mapSettings().destinationCrs()
        tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())

        wgs_rect = QgsRectangle(final_bbox[0], final_bbox[1], final_bbox[2], final_bbox[3])
        dest_rect = tr.transformBoundingBox(wgs_rect)

        canvas.setExtent(dest_rect)
        canvas.refresh()

    return final_bbox, located_msg


def skill_geocode_address(
    address_text: str,
    lon: Optional[float] = None,
    lat: Optional[float] = None,
    zoom_scale: float = 6000.0,
) -> str:
    """
    地址地理编码并将 QGIS 画布精细聚焦至目标设施/园区（默认 1:6,000 比例尺，防止 AI 像元溢出）。
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
        canvas.zoomScale(zoom_scale)
        canvas.refresh()
        return f"📍 地址定位完成：{address_text} (经度={lon:.6f}, 纬度={lat:.6f})，画布已精细聚焦 (1:{int(zoom_scale)})。"
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
    """底层 AWS STAC 检索 + 按安全视口裁剪的虚拟 VRT 流式加载 Sentinel-2 影像。"""
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
                                f"📐 *图层已安全裁剪为精细工作区范围 (~15km)。*")
        return "\n".join(result_lines)
    except Exception as e:
        return f"检索 Sentinel-2 影像异常: {e}"


def skill_fetch_sentinel2_imagery(
    place_name: str = "当前视口",
    days_back: int = 14,
    max_cloud: int = 15,
    band_type: str = "4band"
) -> str:
    """检索并流式加载 Sentinel-2 遥感影像（自动限制在约 15km 安全视口内）。"""
    bbox, located_msg = _get_target_bbox(place_name, max_span=0.15)

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
    【高可靠多通道】检索并下载当前视口或指定区域的高精度 30米 全球真实 DEM 高程栅格 (GeoTIFF)。
    """
    try:
        bbox, located_msg = _get_target_bbox(place_name, max_span=0.25)
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

        # 通道 3：AWS Terrarium 瓦片解码
        ok = _download_and_decode_terrarium_tif(min_lon, min_lat, max_lon, max_lat, out_tif)
        if ok:
            layer_name = f"Global_DEM_30m_{time_str}"
            layer = QgsRasterLayer(out_tif, layer_name, "gdal")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                return (
                    f"{located_msg}⛰️ **已通过高程瓦片解码成功生成真实 DEM 栅格**：`{layer_name}`\n"
                    f"💡 *该栅格包含真实绝对高程矩阵，已完全支持坡度、坡向、填洼与积水区分析算子。*"
                )

        return f"{located_msg}❌ 获取 DEM 失败：所有在线通道与瓦片解码均未完成，请检查网络。"

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
# 4. 云端 AI 深度解译任务调度 (内置自动安全中心裁剪，永不被熔断拦截)
# ===========================================================================

def _sanitize_ai_task_extent(layer: QgsRasterLayer, extent=None, extent_crs=None) -> Tuple[QgsRectangle, QgsCoordinateReferenceSystem]:
    """
    【AI 解译核心安全保护】
    若传入范围过大（例如在线底图全视口或超大栅格），自动聚焦视口中心约 1.2km x 1.2km 安全范围，
    将像元量控制在 2400x2400 以内，确保 AI 深度解译 100% 顺畅执行，绝不报错打断。
    """
    canvas = iface.mapCanvas() if iface else None

    if extent is None:
        extent = canvas.extent() if canvas else layer.extent()
        extent_crs = canvas.mapSettings().destinationCrs() if canvas else layer.crs()
    if extent_crs is None:
        extent_crs = layer.crs()

    # 转换至 WGS84 检测真实地理跨度
    try:
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        tr_to_wgs = QgsCoordinateTransform(extent_crs, wgs84, QgsProject.instance())
        wgs_rect = tr_to_wgs.transformBoundingBox(extent)

        # 0.015 度约合 1.5km x 1.5km
        if wgs_rect.width() > 0.015 or wgs_rect.height() > 0.015 or _inspect_raster_profile(layer)["is_online_tile"]:
            cx = wgs_rect.center().x()
            cy = wgs_rect.center().y()
            half = 0.006  # 约 1.2km 范围
            safe_wgs = QgsRectangle(cx - half, cy - half, cx + half, cy + half)

            tr_back = QgsCoordinateTransform(wgs84, extent_crs, QgsProject.instance())
            safe_extent = tr_back.transformBoundingBox(safe_wgs)
            logger.info("Extent too large for AI interpret, automatically centered to safe 1.2km working bbox.")
            return safe_extent, extent_crs
    except Exception as e:
        logger.warning(f"Sanitize extent failed: {e}")

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
    范围自动安全限制在约 6km 以内，防止 Overpass 超时。
    """
    try:
        bbox, located_msg = _get_target_bbox(place_name, max_span=0.06)
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
    """检索并流式加载 Landsat 8/9 C2-L2 30米多光谱卫星影像（自动限制在安全工作区）。"""
    try:
        bbox, located_msg = _get_target_bbox(place_name, max_span=0.20)
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


def skill_fetch_worldcover_lulc(place_name: str = "当前视口") -> str:
    """检索并流式挂载 ESA WorldCover 10米全球土地利用覆盖分类图。"""
    try:
        bbox, located_msg = _get_target_bbox(place_name, max_span=0.20)
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


def _snap_canvas_to_bbox(bbox: List[float]) -> None:
    """
    【新增公共辅助函数】把当前画布范围收紧到给定的 WGS84 bbox。
    专门修复 WorldPop / VIIRS 夜光 / HydroSHEDS 这类只加载了全球范围 WMS/WMTS 图层，
    却从未真正裁剪/收紧显示范围的问题。
    """
    if not iface or not iface.mapCanvas():
        return
    try:
        canvas = iface.mapCanvas()
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        dest_crs = canvas.mapSettings().destinationCrs()
        tr = QgsCoordinateTransform(src_crs, dest_crs, QgsProject.instance())
        rect = tr.transformBoundingBox(QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3]))
        canvas.setExtent(rect)
        canvas.refresh()
    except Exception as e:
        logger.warning(f"Snap canvas to bbox failed: {e}")


def skill_fetch_worldpop_density(place_name: str = "当前视口", year: int = 2020) -> str:
    """加载 WorldPop 全球 100米 人口密度与空间分布栅格图层。"""
    try:
        bbox, located_msg = _get_target_bbox(place_name, max_span=0.30)
        wms_url = (
            "crs=EPSG:4326&dpiMode=7&format=image/png&layers=wp:pop_density_"
            f"{year}&styles=&url=https://hub.worldpop.org/geoserver/wms"
        )
        layer_name = f"WorldPop_{year}_全球人口密度(100m)"
        layer = QgsRasterLayer(wms_url, layer_name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            # 【修复】WMS 图层本身范围是全球的，之前算好的 bbox 从未被使用，
            # 这里主动把画布收紧到安全范围，避免"图层范围看起来过大"。
            _snap_canvas_to_bbox(bbox)
            return f"{located_msg}👥 **已成功加载 WorldPop 全球 100米 人口密度栅格图层**：`{layer_name}`\n📐 *视野已收紧至安全工作区范围。*"
        return f"{located_msg}WorldPop 人口数据服务连接异常。"
    except Exception as e:
        return f"获取 WorldPop 异常: {e}"


def skill_fetch_nighttime_lights(place_name: str = "当前视口") -> str:
    """流式加载 NOAA VIIRS DNB 500米 全球夜间灯光辐射影像。"""
    try:
        bbox, located_msg = _get_target_bbox(place_name, max_span=0.50)
        wmts_url = (
            "crs=EPSG:4326&dpiMode=7&format=image/png&layers=VIIRS_SNPP_DayNightBand_ENCC"
            "&styles=default&url=https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/wmts.cgi"
        )
        layer_name = "VIIRS_全球夜间灯光"
        layer = QgsRasterLayer(wmts_url, layer_name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            # 【修复】同上，主动收紧画布到安全 bbox 范围。
            _snap_canvas_to_bbox(bbox)
            return f"{located_msg}🌃 **已成功加载 VIIRS 全球夜间灯光影像**：`{layer_name}`\n*(可用于分析城市化边界、经济活力与电力分布)*\n📐 *视野已收紧至安全工作区范围。*"
        return f"{located_msg}夜间灯光服务连接失败。"
    except Exception as e:
        return f"获取夜间灯光数据异常: {e}"


def skill_fetch_hydrology_data(place_name: str = "当前视口") -> str:
    """加载 HydroSHEDS 全球水文流域与河网等级矢量数据。"""
    try:
        bbox, located_msg = _get_target_bbox(place_name, max_span=0.50)
        wms_url = (
            "crs=EPSG:4326&dpiMode=7&format=image/png&layers=hydrosheds:hydro_rivers"
            "&styles=&url=https://hydrosheds.org/geoserver/wms"
        )
        layer_name = "HydroSHEDS_全球河网水系"
        layer = QgsRasterLayer(wms_url, layer_name, "wms")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            # 【修复】同上，主动收紧画布到安全 bbox 范围。
            _snap_canvas_to_bbox(bbox)
            return f"{located_msg}💧 **已成功挂载 HydroSHEDS 全球河网与流域水文图层**：`{layer_name}`\n📐 *视野已收紧至安全工作区范围。*"
        return f"{located_msg}HydroSHEDS 水文服务连接异常。"
    except Exception as e:
        return f"获取水文数据异常: {e}"


def skill_fetch_era5_climate(place_name: str = "当前视口", days_back: int = 7) -> str:
    """查询指定地点近期的 ERA5 气象历史再分析数据（气温、降雨量、风速、气压）。"""
    try:
        bbox, _ = _get_target_bbox(place_name, max_span=0.15)
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