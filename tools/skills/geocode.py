# -*- coding: utf-8 -*-
"""Address geocoding & canvas focus skill."""

from ...core.config import settings
from ...core.constants import TIANDITU_GEOCODER_URL
from ...core.prompts import GEOCODE_NO_KEY
from ...api.http_client import HttpClient
from .common import _geocode_place_bbox
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

from ...core.logger import get_logger


logger = get_logger("tools.skills.geocode")
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
            tk = settings.tianditu_api_key()
            if not tk or len(tk) < 10:
                quick_bbox = _geocode_place_bbox(address_text)
                if quick_bbox:
                    lon = (quick_bbox[0] + quick_bbox[2]) / 2.0
                    lat = (quick_bbox[1] + quick_bbox[3]) / 2.0
                    source_type = "内置核心地名库"
                else:
                    return GEOCODE_NO_KEY
            else:
                ds_data = json.dumps({"keyWord": address_text}, ensure_ascii=False)
                params = {"ds": ds_data, "tk": tk}
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.tianditu.gov.cn/",
                }

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
