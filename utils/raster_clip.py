# -*- coding: utf-8 -*-
"""
Raster clipping utility — crops a raster layer to a bounding box.

Automatically handles CRS transformation when the selection extent
and the raster layer use different coordinate systems.
"""
import os
import tempfile

from qgis.core import (
    QgsRasterFileWriter,
    QgsRasterPipe,
    QgsProject,
    QgsRectangle,
    QgsCoordinateTransform,
)

from ..core.constants import JPEG_QUALITY
from ..core.logger import get_logger

logger = get_logger("utils.raster_clip")


def clip_raster_to_temp(
    raster_layer,
    extent: QgsRectangle,
    extent_crs,
    unique_suffix: str,
) -> str:
    """
    Clip *raster_layer* to *extent* and write to a temporary GeoTIFF.

    If the extent CRS differs from the layer CRS, the extent is
    transformed automatically to prevent blank outputs.
    """
    out_path = os.path.join(
        tempfile.gettempdir(), f"input_clip_{unique_suffix}.tif"
    )
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass

    # Transform extent to layer CRS if needed
    target_extent = QgsRectangle(extent)
    if extent_crs and raster_layer.crs().isValid() and extent_crs != raster_layer.crs():
        try:
            transform = QgsCoordinateTransform(
                extent_crs, raster_layer.crs(), QgsProject.instance()
            )
            target_extent = transform.transformBoundingBox(extent)
            logger.debug(
                "Transformed extent from %s to %s",
                extent_crs.authid(),
                raster_layer.crs().authid(),
            )
        except Exception as err:
            logger.warning("CRS transform failed: %s", err)

    # Strategy 1: GDAL direct translate (fastest)
    gdal_result = _try_gdal_translate(raster_layer, target_extent, out_path)
    if gdal_result:
        return gdal_result

    # Strategy 2: QGIS QgsRasterFileWriter (fallback)
    return _write_with_raster_file_writer(raster_layer, target_extent, out_path)


def _try_gdal_translate(raster_layer, extent, out_path):
    """Attempt fast clipping via GDAL Translate."""
    src_path = raster_layer.source()
    if src_path.startswith("file:///"):
        src_path = src_path[8:]
    src_path = src_path.split("|")[0].split("?")[0].strip("\"' ")

    if not os.path.exists(src_path):
        return None

    try:
        from osgeo import gdal

        band_count, is_16bit = _probe_src_info(src_path)
        proj_win = [
            extent.xMinimum(),
            extent.yMaximum(),
            extent.xMaximum(),
            extent.yMinimum(),
        ]

        jpeg_result = _try_jpeg_compress(src_path, out_path, proj_win, band_count, is_16bit)
        if jpeg_result:
            return jpeg_result

        creation_options = [
            "COMPRESS=DEFLATE",
            "PREDICTOR=2",
            "TILED=YES",
            "BLOCKXSIZE=256",
            "BLOCKYSIZE=256",
        ]
        options = gdal.TranslateOptions(projWin=proj_win, creationOptions=creation_options)
        ds = gdal.Translate(out_path, src_path, options=options)
        ds = None
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as err:
        logger.warning("GDAL Translate failed: %s", err)

    return None


def _probe_src_info(src_path):
    """Probe band count and bit depth from a raster file."""
    band_count = 0
    is_16bit = False
    try:
        from osgeo import gdal

        ds = gdal.Open(src_path, gdal.GA_ReadOnly)
        if ds:
            band_count = ds.RasterCount
            band = ds.GetRasterBand(1)
            if band:
                dtype = gdal.GetDataTypeName(band.DataType)
                if dtype and "16" in dtype:
                    is_16bit = True
            ds = None
    except Exception:
        pass
    return band_count, is_16bit


def _try_jpeg_compress(src_path, out_path, proj_win, band_count, is_16bit):
    """Attempt JPEG-compressed clipping for smaller upload sizes."""
    try:
        from osgeo import gdal

        if band_count >= 3:
            photometric = "YCBCR"
        elif band_count == 1:
            photometric = "MINISBLACK"
        else:
            return None

        creation_options = [
            "COMPRESS=JPEG",
            f"JPEG_QUALITY={JPEG_QUALITY}",
            f"PHOTOMETRIC={photometric}",
            "TILED=YES",
            "BLOCKXSIZE=256",
            "BLOCKYSIZE=256",
        ]

        translate_kwargs = {
            "projWin": proj_win,
            "creationOptions": creation_options,
        }
        if is_16bit:
            translate_kwargs["outputType"] = gdal.GDT_Byte

        options = gdal.TranslateOptions(**translate_kwargs)
        ds = gdal.Translate(out_path, src_path, options=options)
        ds = None
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as err:
        logger.warning("JPEG compress failed: %s", err)

    return None


def _write_with_raster_file_writer(raster_layer, extent, out_path):
    """Fallback clipping via QGIS native QgsRasterFileWriter."""
    try:
        provider = raster_layer.dataProvider()
        pipe = QgsRasterPipe()
        if not pipe.set(provider.clone()):
            raise RuntimeError("无法创建栅格数据管道")

        x_res = raster_layer.rasterUnitsPerPixelX()
        y_res = raster_layer.rasterUnitsPerPixelY()
        if x_res <= 0 or y_res <= 0:
            x_res = y_res = 1.0

        cols = max(1, int(round(extent.width() / x_res)))
        rows = max(1, int(round(extent.height() / y_res)))

        transform_ctx = QgsProject.instance().transformContext()

        # Attempt 1: JPEG compression
        writer = QgsRasterFileWriter(out_path)
        writer.setCreateOptions([
            "COMPRESS=JPEG",
            f"JPEG_QUALITY={JPEG_QUALITY}",
            "PHOTOMETRIC=YCBCR",
            "TILED=YES",
        ])
        error = writer.writeRaster(
            pipe, cols, rows, extent, raster_layer.crs(), transform_ctx
        )
        if (
            error == QgsRasterFileWriter.NoError
            and os.path.exists(out_path)
            and os.path.getsize(out_path) > 0
        ):
            return out_path

        # Attempt 2: DEFLATE compression
        pipe2 = QgsRasterPipe()
        if not pipe2.set(provider.clone()):
            raise RuntimeError("无法创建栅格数据管道 (fallback)")

        writer2 = QgsRasterFileWriter(out_path)
        writer2.setCreateOptions(["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"])
        error2 = writer2.writeRaster(
            pipe2, cols, rows, extent, raster_layer.crs(), transform_ctx
        )
        if (
            error2 == QgsRasterFileWriter.NoError
            and os.path.exists(out_path)
            and os.path.getsize(out_path) > 0
        ):
            return out_path

        raise RuntimeError("QgsRasterFileWriter 写入失败")

    except Exception as err:
        raise RuntimeError(f"图层裁剪失败: {err}")
