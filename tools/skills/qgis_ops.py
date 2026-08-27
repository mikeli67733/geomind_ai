# -*- coding: utf-8 -*-
"""Native QGIS Processing operator search/exec and the guarded dynamic PyQGIS executor."""

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


logger = get_logger("tools.skills.qgis_ops")
# ===========================================================================
# 5. QGIS 原生 Processing 算子检索与防卡死动态执行
# ===========================================================================

def qgis_search_tools(query: str, top_k: int = 5) -> str:
    """语义搜索 QGIS 原生处理算法。"""
    try:
        from ....utils.qgis_indexer import QgisToolVectorIndexer
        indexer = QgisToolVectorIndexer()
        results = indexer.search(query, top_k=top_k)
        if not results:
            return f"未找到与 '{query}' 相关的 QGIS 原生算子。"

        lines = [f"为您检索到最匹配的 {len(results)} 个 QGIS 算子："]
        for r in results:
            lines.append(f"- **ID**: `{r['id']}` | **名称**: {r['name']} ({r['group']})")
            lines.append(f"  *说明*: {r['description']}")
        lines.append("\n可调用 `qgis_get_tool_params(algorithm_id)` 获取入参，随后调用 `qgis_run_algorithm` 执行。")
        return "\n".join(lines)
    except Exception as e:
        return f"检索算子失败: {e}"


def qgis_get_tool_params(algorithm_id: str) -> str:
    """获取指定算法的入参 Schema。"""
    alg = QgsApplication.processingRegistry().algorithmById(algorithm_id)
    if not alg:
        return f"错误：未找到算子 `{algorithm_id}`"

    param_info = [f"算法 `{algorithm_id}` ({alg.displayName()}) 参数列表:"]
    for p in alg.parameterDefinitions():
        req = "必填" if not (p.flags() & QgsProcessingParameterDefinition.FlagOptional) else "选填"
        param_info.append(f"- **{p.name()}** ({p.type()}, {req}): {p.description()} (默认: {p.defaultValue()})")
    return "\n".join(param_info)


def _looks_like_layer_param(alg, param_name: str) -> bool:
    try:
        p = alg.parameterDefinition(param_name)
        if p is None:
            return False
        return p.type() in ("source", "layer", "raster", "vector", "multilayer")
    except Exception:
        return param_name.upper() in ("INPUT", "SOURCE", "LAYER", "LAYER_T1", "LAYER_T2")


def qgis_run_algorithm(algorithm_id: str, parameters: dict) -> str:
    """执行原生 QGIS 算法并自动加载结果。"""
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback

    canvas = iface.mapCanvas() if iface else None
    if canvas:
        canvas.setRenderFlag(False)

    try:
        registry = QgsApplication.processingRegistry()
        alg = registry.createAlgorithmById(algorithm_id)
        if not alg:
            return f"错误：未找到算法 `{algorithm_id}`"

        resolved_params = {}
        missing_layer_names = []
        for k, v in parameters.items():
            if isinstance(v, str):
                layers = QgsProject.instance().mapLayersByName(v)
                if layers:
                    resolved_params[k] = layers[0]
                elif _looks_like_layer_param(alg, k):
                    missing_layer_names.append((k, v))
                    resolved_params[k] = v
                else:
                    resolved_params[k] = v
            else:
                resolved_params[k] = v

        if missing_layer_names:
            detail = ", ".join(f"{k}='{v}'" for k, v in missing_layer_names)
            return f"参数错误：未找到图层 {detail}，请调用 get_active_layers 确认准确图层名。"

        for out in alg.outputDefinitions():
            if out.name() not in resolved_params:
                resolved_params[out.name()] = "memory:"

        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()

        QCoreApplication.processEvents()

        outputs, ok = alg.run(resolved_params, context, feedback)
        if not ok:
            return f"算法 `{alg.displayName()}` ({algorithm_id}) 执行失败：{feedback.textLog() or '无详细日志'}"

        loaded_layers = []
        layers_to_load = dict(context.layersToLoadOnCompletion())
        for layer_id, details in layers_to_load.items():
            layer = context.temporaryLayerStore().mapLayer(layer_id)
            if layer is None:
                layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None and layer.isValid():
                context.temporaryLayerStore().removeMapLayer(layer_id)
                display_name = details.name if getattr(details, "name", None) else layer.name()
                layer.setName(display_name)
                QgsProject.instance().addMapLayer(layer)
                loaded_layers.append(layer.name())

        if not loaded_layers:
            for out_name, out_val in outputs.items():
                layer = None
                if isinstance(out_val, QgsMapLayer):
                    layer = out_val
                elif isinstance(out_val, str) and out_val:
                    layer = context.temporaryLayerStore().mapLayer(out_val)
                    if layer is None and os.path.exists(out_val):
                        lower = out_val.lower()
                        if lower.endswith((".tif", ".tiff", ".img")):
                            cand = QgsRasterLayer(out_val, f"{alg.displayName()}_结果")
                            layer = cand if cand.isValid() else None
                        elif lower.endswith((".shp", ".gpkg", ".geojson")):
                            cand = QgsVectorLayer(out_val, f"{alg.displayName()}_结果", "ogr")
                            layer = cand if cand.isValid() else None

                if layer is not None and layer.isValid():
                    context.temporaryLayerStore().takeMapLayer(layer)
                    QgsProject.instance().addMapLayer(layer)
                    loaded_layers.append(layer.name())

        if not loaded_layers:
            return f"算法 `{alg.displayName()}` 执行完成，但未捕获到有效图层。"

        return f"算法 `{alg.displayName()}` 执行成功，已加载图层: {', '.join(loaded_layers)}"
    except Exception as e:
        logger.error("Algorithm execution failed: %s", e)
        return f"执行算子 `{algorithm_id}` 失败: {e}"
    finally:
        if canvas:
            canvas.setRenderFlag(True)
            canvas.refresh()
        QCoreApplication.processEvents()


# ===========================================================================
# 6. PyQGIS 动态代码执行器 (防假死与内存沙箱强化版)
# ===========================================================================

# Modules the dynamic executor may import. Everything else (subprocess,
# socket, urllib, ctypes, shutil, ...) is denied — LLM-generated code must
# not reach the network, spawn processes or mutate files outside temp/QGIS.
_SANDBOX_ALLOWED_IMPORTS = frozenset({
    "qgis", "processing", "numpy", "gdal", "osgeo", "json",
    "math", "datetime", "re", "io", "tempfile", "os.path", "collections",
})

# Builtins withheld from dynamic code: dynamic execution machinery and
# attribute traps used to escape restricted namespaces.
_SANDBOX_DENIED_BUILTINS = ("exec", "eval", "compile", "__import__", "globals",
                            "locals", "vars", "delattr", "exit", "quit")


def _make_sandbox_builtins():
    import builtins as _builtins
    safe = {k: getattr(_builtins, k) for k in dir(_builtins)
            if not k.startswith("_") and k not in _SANDBOX_DENIED_BUILTINS}
    safe["__build_class__"] = _builtins.__build_class__
    safe["__name__"] = "sandbox"
    return safe


def _make_sandbox_import(default_import=None):
    """Return an __import__ that only resolves allow-listed top-level modules."""
    real_import = default_import

    def _sandboxed(name, globs=None, locs=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root == "os" and name != "os.path":
            raise ImportError(f"沙箱禁止导入模块: {name}")
        if root not in _SANDBOX_ALLOWED_IMPORTS:
            raise ImportError(f"沙箱禁止导入模块: {name}")
        return real_import(name, globs, locs, fromlist, level)

    return _sandboxed


def execute_pyqgis_code(python_code: str) -> str:
    """
    【防卡死 + 模块白名单安全强化版】在当前 QGIS 环境中动态执行 Python/PyQGIS 代码。

    防护措施（尽力而为，非硬隔离）：
    1. ``import`` 走自定义钩子，仅允许地理/数值分析类模块；
    2. 内建函数剔除 exec/eval/compile/__import__ 等逃逸原语。
    """
    import io
    import sys
    import traceback
    import processing
    import qgis.core as qgis_core

    canvas = iface.mapCanvas() if iface else None
    if canvas:
        canvas.setRenderFlag(False)

    sandbox_builtins = _make_sandbox_builtins()
    sandbox_builtins["__import__"] = _make_sandbox_import(__import__)

    # 常用符号直接注入，避免正常脚本因 import 受限而失败
    exec_globals = {
        "__builtins__": sandbox_builtins,
        "QgsProject": qgis_core.QgsProject,
        "QgsVectorLayer": qgis_core.QgsVectorLayer,
        "QgsRasterLayer": qgis_core.QgsRasterLayer,
        "QgsFeature": qgis_core.QgsFeature,
        "QgsGeometry": qgis_core.QgsGeometry,
        "QgsPointXY": qgis_core.QgsPointXY,
        "QgsRectangle": qgis_core.QgsRectangle,
        "QgsField": qgis_core.QgsField,
        "QgsCoordinateTransform": qgis_core.QgsCoordinateTransform,
        "QgsCoordinateReferenceSystem": qgis_core.QgsCoordinateReferenceSystem,
        "QgsFeatureRequest": qgis_core.QgsFeatureRequest,
        "QgsSpatialIndex": qgis_core.QgsSpatialIndex,
        "edit": qgis_core.edit,
        "processEvents": QCoreApplication.processEvents,
        "iface": iface,
        "processing": processing,
        "np": np,
        "numpy": np,
        "gdal": gdal,
        "ogr": ogr,
        "osr": osr,
        "json": json,
        "math": math,
    }

    stdout_backup = sys.stdout
    captured_output = io.StringIO()
    sys.stdout = captured_output

    try:
        QCoreApplication.processEvents()
        exec(python_code, exec_globals)  # noqa: S102 - guarded by sandbox hooks above
        sys.stdout = stdout_backup

        logs = captured_output.getvalue().strip()
        log_str = f"\n执行日志输出:\n{logs}" if logs else ""
        return f"✔ PyQGIS 代码执行成功！{log_str}"

    except Exception as e:
        sys.stdout = stdout_backup
        err_detail = traceback.format_exc()
        return f"❌ PyQGIS 代码执行报错: {e}\n详细堆栈:\n{err_detail}"

    finally:
        if canvas:
            canvas.setRenderFlag(True)
            canvas.refresh()
        QCoreApplication.processEvents()
