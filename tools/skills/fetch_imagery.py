# -*- coding: utf-8 -*-
"""Online satellite imagery acquisition skills:
Sentinel-2, Copernicus DEM, Landsat 8/9, Sentinel-1 SAR (multi-source STAC/VRT streaming)."""

STAC_AWS_URL = "https://earth-search.aws.element84.com/v1/search"
STAC_MPC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
MPC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

from .common import _get_target_bbox, _download_and_decode_terrarium_tif, _HTTP

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

from ...core.logger import get_logger


logger = get_logger("tools.skills.fetch_imagery")
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
        resp = _HTTP.post(STAC_AWS_URL, json=payload, timeout=15)
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
            resp = _HTTP.get(ot_url, params=params, timeout=10)
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
            resp = _HTTP.post(STAC_AWS_URL, json=payload, timeout=8)
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
        resp = _HTTP.post(STAC_AWS_URL, json=payload, timeout=15)
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


def _mpc_sign_href(href: str) -> str:
    """为 Planetary Computer 上的受保护 blob 资产签发临时可匿名访问的 SAS URL。"""
    try:
        resp = _HTTP.get(MPC_SIGN_URL, params={"href": href}, timeout=10)
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
        resp = _HTTP.post(STAC_MPC_URL, json=payload, timeout=15)
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
