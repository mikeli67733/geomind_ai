# -*- coding: utf-8 -*-
"""Global thematic open-data skills: Natural Earth, WorldCover, WorldPop,
nighttime lights, HydroSHEDS and ERA5 climate."""

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

from .common import _get_target_bbox, _HTTP


logger = get_logger("tools.skills.fetch_thematic")
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
        resp = _HTTP.get(url, params=params, timeout=12)
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
