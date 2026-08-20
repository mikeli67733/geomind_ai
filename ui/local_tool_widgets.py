# -*- coding: utf-8 -*-
"""
Compatibility shim for the former monolithic local tool widgets module.

v5.0 splits the 10 tool pages into the ``ui.local_tools`` package.
This module only re-exports the widget classes so that existing imports
(``from .local_tool_widgets import SpectralIndexTaskWidget``) keep working.
New code should import from ``..local_tools`` instead.
"""
from .local_tools import (
    BaseLocalToolWidget,
    SpectralIndexTaskWidget,
    PcaTransformWidget,
    DemAnalysisWidget,
    SpatialFilterWidget,
    AreaStatisticsWidget,
    VectorSmoothSimplifyWidget,
    KMeansClusterWidget,
    RasterDiffChangeWidget,
    ImageEnhanceWidget,
    RasterPolygonizeWidget,
    LOCAL_TOOL_PAGES,
    create_local_tool_page,
)

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
