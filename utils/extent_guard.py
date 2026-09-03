# -*- coding: utf-8 -*-
"""
Extent size guard — estimates the pixel footprint of a selection extent
against a raster layer and reports whether it is too large to interpret.

Used before clipping/uploading cloud interpretation tasks so oversized
viewport ranges are rejected early instead of timing out or failing
mid-flight. Applies to both the dock UI pages and the Copilot skills.
"""
from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    QgsProject,
    QgsRectangle,
    QgsCoordinateTransform,
)

from ..core.constants import (
    MAX_EXTENT_PIXELS,
    MAX_EXTENT_SIDE_PIXELS,
)
from ..core.logger import get_logger

logger = get_logger("utils.extent_guard")


def is_local_gateway_mode() -> bool:
    """
    检查当前是否配置为本地/私有化网关模式。
    读取与 AccountSettingsPage 一致的 QSettings 配置项。
    """
    try:
        settings = QSettings()
        mode = settings.value("GeoMind/gateway_mode", "cloud")
        return mode == "custom"
    except Exception as err:
        logger.debug("Failed to read gateway settings: %s", err)
        return False


def estimate_extent_pixels(layer, extent, extent_crs=None):
    """
    Estimate the extent footprint in pixels of *extent* on *layer*.

    Returns ``(cols, rows)`` or ``None`` when the estimate is impossible
    (non-raster layer, invalid/zero resolution, CRS transform failure).
    The extent is transformed into the layer CRS first when needed so the
    estimate stays meaningful across projections.
    """
    try:
        target = QgsRectangle(extent)
        layer_crs = layer.crs()
        if extent_crs is not None and layer_crs.isValid() and extent_crs != layer_crs:
            transform = QgsCoordinateTransform(
                extent_crs, layer_crs, QgsProject.instance()
            )
            target = transform.transformBoundingBox(extent)

        x_res = layer.rasterUnitsPerPixelX()
        y_res = layer.rasterUnitsPerPixelY()
        if x_res <= 0 or y_res <= 0:
            return None

        cols = max(1, int(round(target.width() / x_res)))
        rows = max(1, int(round(target.height() / y_res)))
        return cols, rows
    except Exception as err:
        logger.debug("Extent pixel estimate failed: %s", err)
        return None


def check_extent_too_large(
    layer,
    extent,
    extent_crs=None,
    max_pixels: int = MAX_EXTENT_PIXELS,
    max_side: int = MAX_EXTENT_SIDE_PIXELS,
    is_local: bool = None,
):
    """
    Check whether *extent* is too large to interpret on *layer*.

    :param is_local: 是否为本地服务。如果为 None，则会自动通过
                     QSettings 判断当前是否处于本地/私有网关模式。
                     本地模式下直接取消限制，返回 (False, "")。

    Returns ``(is_too_large, message)``.
    """
    # 1. 判断是否为本地模式：未显式传入时自动从配置读取
    if is_local is None:
        is_local = is_local_gateway_mode()

    # 2. 本地模式：彻底取消限制，允许任意大小解译
    if is_local:
        logger.debug("当前为本地/私有服务模式，已跳过解译范围尺寸限制。")
        return False, ""

    # 3. 远程云端模式：执行原有的严格尺寸校验
    size = estimate_extent_pixels(layer, extent, extent_crs)
    if size is None:
        return False, ""

    cols, rows = size
    pixels = cols * rows
    if pixels <= max_pixels and cols <= max_side and rows <= max_side:
        return False, ""

    limit_mp = max_pixels / 1_000_000
    # Rough JPEG (q75) size estimate for remote-sensing imagery ~0.8 B/px.
    est_mb = pixels * 0.8 / 1_000_000
    msg = (
        f"当前解译范围约 {cols:,} × {rows:,} 像素（约 {pixels / 1_000_000:.1f} 百万像素，"
        f"预计上传约 {est_mb:.0f} MB），"
        f"已超过带宽支持上限（{limit_mp:.0f} 百万像素，单边 {max_side:,} 像素）。"
        "范围过大容易导致裁剪超时、上传失败或解译质量下降。"
    )
    logger.info("Extent too large: %s x %s px", cols, rows)
    return True, msg