# -*- coding: utf-8 -*-
"""
Local tool page registry and factory.

Each entry maps a stable page key to a display title and a widget class.
The dock widget iterates this registry to build navigation, so adding a
new tool only requires appending one entry here.
"""
from typing import Callable, List, Tuple

from qgis.PyQt.QtWidgets import QWidget

from .spectral_index import SpectralIndexTaskWidget
from .pca import PcaTransformWidget
from .dem import DemAnalysisWidget
from .filter import SpatialFilterWidget
from .area_stats import AreaStatisticsWidget
from .vector_smooth import VectorSmoothSimplifyWidget
from .kmeans import KMeansClusterWidget
from .raster_diff import RasterDiffChangeWidget
from .enhance import ImageEnhanceWidget
from .polygonize import RasterPolygonizeWidget

#: (page_key, display_title, widget_factory(main_dock) -> QWidget)
LOCAL_TOOL_PAGES: List[Tuple[str, str, Callable]] = [
    ("task_spectral_index", "🍀 全能光谱指数库", SpectralIndexTaskWidget),
    ("task_pca", "🔮 PCA 主成分分析", PcaTransformWidget),
    ("task_dem", "🗻 DEM 地形全要素分析", DemAnalysisWidget),
    ("task_filter", "🔎 空间滤波与边缘提取", SpatialFilterWidget),
    ("task_area", "🍰 地物分类面积统计", AreaStatisticsWidget),
    ("task_vector_smooth", "🎀 矢量图斑化简平滑", VectorSmoothSimplifyWidget),
    ("task_kmeans", "🍭 K-Means 智能聚类", KMeansClusterWidget),
    ("task_raster_diff", "🐣 双期像元差分检测", RasterDiffChangeWidget),
    ("task_enhance", "🌈 假彩色画质增强", ImageEnhanceWidget),
    ("task_polygonize", "🧩 栅格一键矢量化", RasterPolygonizeWidget),
]


def create_local_tool_page(page_key: str, main_dock) -> QWidget:
    """Instantiate a local tool page by its registry key."""
    for key, _title, factory in LOCAL_TOOL_PAGES:
        if key == page_key:
            return factory(main_dock)
    raise KeyError(f"Unknown local tool page: {page_key}")
