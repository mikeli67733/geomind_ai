# -*- coding: utf-8 -*-
"""
PyQt5 / PyQt6 and QGIS version compatibility layer.

Centralises all fragile ``getattr`` fallbacks so individual modules
don't need to repeat the same boilerplate.
"""
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsMapLayerProxyModel, QgsTask, QgsWkbTypes

# -- Qt enum compatibility --------------------------------------------------

LEFT_BUTTON = getattr(Qt, "LeftButton", None)
if LEFT_BUTTON is None:
    LEFT_BUTTON = Qt.MouseButton.LeftButton

RIGHT_BUTTON = getattr(Qt, "RightButton", None)
if RIGHT_BUTTON is None:
    RIGHT_BUTTON = Qt.MouseButton.RightButton

KEY_ESCAPE = getattr(Qt, "Key_Escape", None)
if KEY_ESCAPE is None:
    KEY_ESCAPE = Qt.Key.Key_Escape

ALIGN_CENTER = getattr(Qt, "AlignCenter", None)
if ALIGN_CENTER is None:
    ALIGN_CENTER = Qt.AlignmentFlag.AlignCenter

KEEP_ASPECT_RATIO = getattr(Qt, "KeepAspectRatio", None)
if KEEP_ASPECT_RATIO is None:
    KEEP_ASPECT_RATIO = Qt.AspectRatioMode.KeepAspectRatio

SMOOTH_TRANSFORMATION = getattr(Qt, "SmoothTransformation", None)
if SMOOTH_TRANSFORMATION is None:
    SMOOTH_TRANSFORMATION = Qt.TransformationMode.SmoothTransformation

# -- QGIS layer filter compatibility ----------------------------------------

RASTER_LAYER_FILTER = getattr(QgsMapLayerProxyModel, "RasterLayer", None)
if RASTER_LAYER_FILTER is None:
    try:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.RasterLayer
    except AttributeError:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.Raster

VECTOR_LAYER_FILTER = getattr(QgsMapLayerProxyModel, "VectorLayer", None)
if VECTOR_LAYER_FILTER is None:
    try:
        VECTOR_LAYER_FILTER = QgsMapLayerProxyModel.Filter.VectorLayer
    except AttributeError:
        VECTOR_LAYER_FILTER = QgsMapLayerProxyModel.Filter.Vector

# -- QGIS task flag compatibility -------------------------------------------

TASK_CAN_CANCEL = getattr(QgsTask, "CanCancel", None)
if TASK_CAN_CANCEL is None:
    TASK_CAN_CANCEL = QgsTask.Flag.CanCancel

# -- QGIS geometry type compatibility ---------------------------------------

POLYGON_GEOMETRY = getattr(QgsWkbTypes, "PolygonGeometry", None)
if POLYGON_GEOMETRY is None:
    POLYGON_GEOMETRY = QgsWkbTypes.GeometryType.PolygonGeometry
