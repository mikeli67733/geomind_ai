# -*- coding: utf-8 -*-
"""
Shared vector processing operations.

Currently wraps QGIS processing algorithms for geometry simplification
and smoothing.  Used by both UI widgets and the LLM skill dispatcher.
"""
from qgis.core import QgsProject, QgsVectorLayer

from ..core.logger import get_logger

logger = get_logger("tools.vector_ops")


def vector_simplify_and_smooth(
    layer: QgsVectorLayer,
    tolerance: float = 1.0,
    iterations: int = 2,
) -> QgsVectorLayer:
    """
    Simplify and smooth vector geometries.

    Wraps ``native:simplifygeometries`` followed by ``native:smoothgeometry``.
    """
    from qgis import processing

    res_simp = processing.run(
        "native:simplifygeometries",
        {
            "INPUT": layer,
            "METHOD": 0,
            "TOLERANCE": tolerance,
            "OUTPUT": "memory:",
        },
    )
    res_smooth = processing.run(
        "native:smoothgeometry",
        {
            "INPUT": res_simp["OUTPUT"],
            "ITERATIONS": iterations,
            "OFFSET": 0.25,
            "OUTPUT": "memory:",
        },
    )
    out_layer = res_smooth["OUTPUT"]
    return out_layer
