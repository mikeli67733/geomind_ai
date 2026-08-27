# -*- coding: utf-8 -*-
"""
Skill implementations, split from the former monolithic skill_dispatcher.

Importing this package registers every whitelisted LLM-callable skill in
``tools.skill_registry`` and is the single source of the whitelist.
"""
from ..skill_registry import register

from . import layers as _layers
from . import geocode as _geocode
from . import fetch_imagery as _imagery
from . import fetch_vector as _vector
from . import fetch_thematic as _thematic
from . import analysis as _analysis
from . import ai_tasks as _ai
from . import qgis_ops as _qgis
from . import web as _web

_A = _analysis
_FI = _imagery
_FT = _thematic
_Q = _qgis


register("get_active_layers", _layers.get_active_layers,
         "读取当前图层列表与物理画像", "local")
register("skill_geocode_address", _geocode.skill_geocode_address,
         "地名地址解析与地图定位", "local")

register("skill_fetch_sentinel2_imagery", _FI.skill_fetch_sentinel2_imagery,
         "检索并流式加载 Sentinel-2 影像", "local")
register("skill_fetch_dem_data", _FI.skill_fetch_dem_data,
         "检索并加载 30m 全球 Copernicus DEM", "local")
register("skill_fetch_landsat_imagery", _FI.skill_fetch_landsat_imagery,
         "流式检索加载 Landsat 8/9 影像", "local")
register("skill_fetch_sentinel1_sar", _FI.skill_fetch_sentinel1_sar,
         "检索并流式加载 Sentinel-1 SAR 雷达影像", "local")

register("skill_fetch_osm_vector_data", _vector.skill_fetch_osm_vector_data,
         "获取 OpenStreetMap 真实矢量 JSON 数据", "local")

register("skill_fetch_natural_earth", _FT.skill_fetch_natural_earth,
         "加载 Natural Earth 全球基础矢量", "local")
register("skill_fetch_worldcover_lulc", _FT.skill_fetch_worldcover_lulc,
         "加载 ESA 10m 全球土地利用覆盖", "local")
register("skill_fetch_worldpop_density", _FT.skill_fetch_worldpop_density,
         "加载 WorldPop 全球人口密度数据", "local")
register("skill_fetch_nighttime_lights", _FT.skill_fetch_nighttime_lights,
         "加载 VIIRS 全球夜间灯光影像", "local")
register("skill_fetch_hydrology_data", _FT.skill_fetch_hydrology_data,
         "加载 HydroSHEDS 全球水文河网", "local")
register("skill_fetch_era5_climate", _FT.skill_fetch_era5_climate,
         "查询 ECMWF ERA5 气象气候数据", "local")

register("skill_raster_threshold", _A.skill_raster_threshold,
         "栅格指数阈值二值化提取", "local")
register("skill_run_pca", _A.skill_run_pca,
         "执行主成分分析 (PCA)", "local")
register("skill_dem_analysis", _A.skill_dem_analysis,
         "分析 DEM 地形要素", "local")
register("skill_spatial_filter", _A.skill_spatial_filter,
         "执行空间滤波与边缘提取", "local")
register("skill_area_statistics", _A.skill_area_statistics,
         "统计地物分类与图斑面积", "local")
register("skill_vector_smooth", _A.skill_vector_smooth,
         "平滑与化简矢量图斑", "local")
register("skill_kmeans_cluster", _A.skill_kmeans_cluster,
         "执行 K-Means 聚类分析", "local")
register("skill_raster_diff", _A.skill_raster_diff,
         "双期像元差分变化检测", "local")
register("skill_image_enhance", _A.skill_image_enhance,
         "影像画质增强与真/假彩色合成", "local")
register("skill_raster_polygonize", _A.skill_raster_polygonize,
         "栅格结果矢量化与面要素提取", "local")

register("skill_ai_extract_feature", _ai.skill_ai_extract_feature,
         "启动云端标准地物解译模型", "ai")
register("skill_ai_sam3_extract", _ai.skill_ai_sam3_extract,
         "启动云端 SAM3 交互提示解译", "ai")
register("skill_ai_change_detection", _ai.skill_ai_change_detection,
         "启动云端深度时相变化检测模型", "ai")

register("qgis_search_tools", _Q.qgis_search_tools,
         "检索 QGIS 空间算法工具箱", "qgis")
register("qgis_get_tool_params", _Q.qgis_get_tool_params,
         "读取 QGIS 算法参数配置", "qgis")
register("qgis_run_algorithm", _Q.qgis_run_algorithm,
         "执行 QGIS 本地空间分析算法", "qgis")
register("execute_pyqgis_code", _Q.execute_pyqgis_code,
         "执行动态 PyQGIS 空间分析代码", "qgis")

register("skill_web_search", _web.skill_web_search,
         "联网检索实时资讯与专业文档", "web")
register("skill_fetch_webpage_content", _web.skill_fetch_webpage_content,
         "抓取并解析网页正文内容", "web")


# Compatibility re-exports (legacy code imports these from tools.skills)
from .layers import get_layer_by_name, _inspect_raster_profile  # noqa: F401
from .common import _validate_bbox, _get_target_bbox  # noqa: F401
from .fetch_imagery import search_and_load_sentinel2, STAC_AWS_URL  # noqa: F401
from .fetch_vector import OVERPASS_SERVERS  # noqa: F401

__all__ = [
    "get_layer_by_name",
    "_inspect_raster_profile",
    "_validate_bbox",
    "_get_target_bbox",
    "search_and_load_sentinel2",
    "STAC_AWS_URL",
    "OVERPASS_SERVERS",
]
