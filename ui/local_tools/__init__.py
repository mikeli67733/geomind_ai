# -*- coding: utf-8 -*-
"""
Local (offline) GIS/RS tool pages package.

Each tool lives in its own module under ``ui.local_tools`` and is
registered in :mod:`ui.local_tools.registry` for the dock navigation.
"""
from .base import BaseLocalToolWidget
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
from .registry import LOCAL_TOOL_PAGES, create_local_tool_page

__all__ = [
    "BaseLocalToolWidget",
    "SpectralIndexTaskWidget",
    "PcaTransformWidget",
    "DemAnalysisWidget",
    "SpatialFilterWidget",
    "AreaStatisticsWidget",
    "VectorSmoothSimplifyWidget",
    "KMeansClusterWidget",
    "RasterDiffChangeWidget",
    "ImageEnhanceWidget",
    "RasterPolygonizeWidget",
    "LOCAL_TOOL_PAGES",
    "create_local_tool_page",
]
