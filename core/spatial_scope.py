# -*- coding: utf-8 -*-
"""
进程级"当前活动范围"共享状态。

主页（Copilot 页）通过 ``dock.set_global_selection()`` 框选/选定范围时，会把
该范围镜像写入这里；任何不便直接依赖 UI dock 对象的模块（tools/skills/*、
tasks/* 等）都可以从这里读取，作为「回退到 canvas.extent()（当前地图视口）
之前」的优先来源 —— 从而让云端 AI 解译、云端数据抓取、本地栅格工具等所有
任务都统一遵循用户在主页框选的范围，而不是各自悄悄退回当前视口。
"""
from typing import Optional, Tuple

from qgis.core import QgsCoordinateReferenceSystem, QgsRectangle

_active_extent: Optional[QgsRectangle] = None
_active_extent_crs: Optional[QgsCoordinateReferenceSystem] = None


def set_active_extent(extent: Optional[QgsRectangle],
                       crs: Optional[QgsCoordinateReferenceSystem] = None) -> None:
    """由主页选择/框选范围时调用，镜像写入共享状态。"""
    global _active_extent, _active_extent_crs
    _active_extent = QgsRectangle(extent) if extent is not None else None
    _active_extent_crs = crs


def get_active_extent() -> Tuple[Optional[QgsRectangle], Optional[QgsCoordinateReferenceSystem]]:
    """返回 (extent, crs)；均可能为 None（表示用户尚未在主页框选范围）。"""
    return _active_extent, _active_extent_crs


def clear_active_extent() -> None:
    """登出/切换工程等场景下清空，避免残留上一次的框选范围。"""
    global _active_extent, _active_extent_crs
    _active_extent = None
    _active_extent_crs = None
