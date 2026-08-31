# -*- coding: utf-8 -*-
"""Cloud AI deep-interpretation task factories (submit-only; run as QgsTask)."""

from ...core.constants import find_class_ids_by_keywords, get_model_key_by_mode
from .layers import get_layer_by_name, _inspect_raster_profile
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
from ...core import spatial_scope


logger = get_logger("tools.skills.ai_tasks")
def _sanitize_ai_task_extent(layer: QgsRasterLayer, extent=None, extent_crs=None) -> Tuple[QgsRectangle, QgsCoordinateReferenceSystem]:
    """提取解译区域范围。"""
    canvas = iface.mapCanvas() if iface else None

    if extent is None:
        # 兜底：调用方未显式传入范围时，优先用主页框选的范围，
        # 而不是直接退回当前地图视口。
        scoped_rect, scoped_crs = spatial_scope.get_active_extent()
        if scoped_rect is not None:
            extent = scoped_rect
            extent_crs = extent_crs or scoped_crs
        else:
            extent = canvas.extent() if canvas else layer.extent()
            extent_crs = canvas.mapSettings().destinationCrs() if canvas else layer.crs()
    if extent_crs is None:
        extent_crs = layer.crs()

    return extent, extent_crs


def skill_ai_extract_feature(
    layer_name: str, feature_type: str, server_url: str, token: str,
    machine_id: str, extent=None, extent_crs=None,
):
    """【卫星影像标准地物模型】建筑/水体/道路/林地/草地/耕地/施工。"""
    from ...tasks.interpret_task import InterpretTask

    layer = get_layer_by_name(layer_name, "raster")
    profile = _inspect_raster_profile(layer)

    if profile["resolution_m"] < 0.35:
        logger.warning("无人机厘米级影像建议改用 SAM3。")

    target_class_id = find_class_ids_by_keywords([feature_type], fallback_id="5")
    real_model_key = get_model_key_by_mode("landuse", fallback_key="LANDUSE")

    task_extent, task_extent_crs = _sanitize_ai_task_extent(layer, extent, extent_crs)

    return InterpretTask(
        raster_layer=layer, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class=target_class_id, prompt="",
        output_format="mask", server_url=server_url, machine_id=machine_id, token=token,
    )


def skill_ai_sam3_extract(
    layer_name: str, prompt: str, output_format: str, server_url: str, token: str,
    machine_id: str, extent=None, extent_crs=None,
):
    """【无人机最高优先 / 万物识别模型】基于 SAM3 的提示词分割与检测。"""
    from ...tasks.interpret_task import InterpretTask

    layer = get_layer_by_name(layer_name, "raster")
    real_model_key = get_model_key_by_mode("sam3", fallback_key="SAM3_MODEL")

    task_extent, task_extent_crs = _sanitize_ai_task_extent(layer, extent, extent_crs)

    return InterpretTask(
        raster_layer=layer, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class="", prompt=prompt,
        output_format=output_format, server_url=server_url, machine_id=machine_id, token=token,
    )


def skill_ai_change_detection(
    layer_t1: str, layer_t2: str, server_url: str, token: str, machine_id: str,
    extent=None, extent_crs=None,
):
    """AI 双期时序变化检测。"""
    from ...tasks.interpret_task import InterpretTask

    l1 = get_layer_by_name(layer_t1, "raster")
    l2 = get_layer_by_name(layer_t2, "raster")
    real_model_key = get_model_key_by_mode("change_detection", fallback_key="CHANGE_DETECTION")

    task_extent, task_extent_crs = _sanitize_ai_task_extent(l1, extent, extent_crs)

    return InterpretTask(
        raster_layer=l1, raster_layer_after=l2, extent=task_extent, extent_crs=task_extent_crs,
        model_key=real_model_key, target_class="", prompt="", output_format="mask",
        server_url=server_url, machine_id=machine_id, token=token,
    )
