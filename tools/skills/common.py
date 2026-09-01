# -*- coding: utf-8 -*-
"""Shared spatial helpers: bbox validation/geocoding fallbacks, viewport extraction,
tile math and terrain-tile decoding. All skill modules import from here."""

from ..place_bboxes import CITY_BBOXES
from ...core import spatial_scope
import os
import re
import json
import math
import tempfile
import urllib.parse
import concurrent.futures
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

from ...core.logger import get_logger


logger = get_logger("tools.skills.common")

#: 模块级共享 HTTP 会话（连接池 + keep-alive），供各技能复用，
#: 避免每次请求都重建 TCP/TLS 连接。
_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": "GeoMind-QGIS-Plugin/1.0"})


def _fetch_tile(url: str) -> Optional[np.ndarray]:
    """下载单张高程瓦片并解码为 RGB 数组；失败返回 None。"""
    try:
        r = _HTTP.get(url, timeout=8)
        if r.status_code == 200:
            return np.array(Image.open(BytesIO(r.content)).convert("RGB"))
    except Exception:
        return None
    return None


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
    quick_bboxes = CITY_BBOXES
    for k, v in quick_bboxes.items():
        if place_name in (k, f"{k}市"):
            return _validate_bbox(v)

    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": place_name, "format": "json", "limit": 1}
        r = _HTTP.get(url, params=params, timeout=5)
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
    不再按地名/地址解析范围，统一采用：
    1. 优先使用主页框选的范围；
    2. 否则精确提取当前 QGIS 画布真实的可见范围矩形，不做多余截断。
    （place_name 参数仅为兼容保留，不再参与范围确定。）
    """
    canvas = iface.mapCanvas() if iface else None
    located_msg = ""
    bbox = None

    # 1. 优先使用主页框选的范围，其次才退回当前屏幕视口
    scoped_rect, scoped_crs = spatial_scope.get_active_extent()
    if scoped_rect is not None:
        rect = scoped_rect
        src_crs = scoped_crs or (canvas.mapSettings().destinationCrs() if canvas else None)
        located_msg = "📍 已使用主页框选的范围\n"
    elif canvas:
        rect = canvas.extent()
        src_crs = canvas.mapSettings().destinationCrs()
    else:
        rect = None
        src_crs = None

    if rect is not None and src_crs is not None:
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

        # 并行下载全部瓦片（共享连接池 + 最多 16 并发），单张失败不影响整体
        tile_jobs = []
        for j, y in enumerate(range(y_min, y_max + 1)):
            for i, x in enumerate(range(x_min, x_max + 1)):
                url = (f"https://s3.amazonaws.com/elevation-tiles-prod/"
                       f"terrarium/{zoom}/{x}/{y}.png")
                tile_jobs.append((j, i, url))

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(16, len(tile_jobs))) as pool:
            futures = {pool.submit(_fetch_tile, url): (j, i)
                       for j, i, url in tile_jobs}
            for fut in concurrent.futures.as_completed(futures):
                j, i = futures[fut]
                tile_img = fut.result()
                if tile_img is not None:
                    full_image[j*tile_size:(j+1)*tile_size,
                               i*tile_size:(i+1)*tile_size] = tile_img

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
