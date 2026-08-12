# -*- coding: utf-8 -*-
"""
原生栅格裁剪逻辑（含动态坐标系转换）
"""

import os
import tempfile
from qgis.core import (
    QgsRasterFileWriter, QgsRasterPipe, QgsProject, QgsRectangle,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem
)

JPEG_QUALITY = 75


def clip_raster_to_temp(raster_layer, extent: QgsRectangle, extent_crs: QgsCoordinateReferenceSystem, unique_suffix: str) -> str:
    """
    将 raster_layer 中 extent 范围内的像元裁剪到一个临时 TIFF 文件。
    【关键修复】：自动将 extent 转换到当前 raster_layer 的 CRS，解决 T1/T2 坐标系不一致导致切出黑屏的问题！
    """
    out_path = os.path.join(tempfile.gettempdir(), f"input_clip_{unique_suffix}.tif")
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass

    # 👈 【核心修复】：如果框选范围的坐标系与当前图层不一致，先进行坐标投影转换！
    target_extent = QgsRectangle(extent)
    if extent_crs and raster_layer.crs().isValid() and extent_crs != raster_layer.crs():
        try:
            transform = QgsCoordinateTransform(extent_crs, raster_layer.crs(), QgsProject.instance())
            target_extent = transform.transformBoundingBox(extent)
            print(f"[raster_clip] 已将框选范围从 {extent_crs.authid()} 转换至图层 CRS: {raster_layer.crs().authid()}")
        except Exception as err:
            print(f"[raster_clip] 坐标系转换异常: {err}")

    # 使用转换后的 target_extent 进行裁切
    gdal_result = _try_gdal_translate(raster_layer, target_extent, out_path)
    if gdal_result:
        return gdal_result

    return _write_with_raster_file_writer(raster_layer, target_extent, out_path)


def _try_gdal_translate(raster_layer, extent, out_path):
    """方案 1: 清理 URI 并使用 GDAL 直读快速裁切"""
    src_path = raster_layer.source()
    if src_path.startswith("file:///"):
        src_path = src_path[8:]
    src_path = src_path.split("|")[0].split("?")[0].strip('"\' ')

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
            "BLOCKYSIZE=256"
        ]
        options = gdal.TranslateOptions(projWin=proj_win, creationOptions=creation_options)
        ds = gdal.Translate(out_path, src_path, options=options)
        ds = None
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
    except Exception as err:
        print(f"GDAL Direct Translate Failed: {err}")

    return None


def _probe_src_info(src_path):
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
        print(f"JPEG compress failed: {err}")

    return None


def _write_with_raster_file_writer(raster_layer, extent, out_path):
    """方案 2: 使用 QGIS 内置 QgsRasterFileWriter 原生 C++ 管道写盘"""
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

        create_options_jpeg = [
            "COMPRESS=JPEG",
            f"JPEG_QUALITY={JPEG_QUALITY}",
            "PHOTOMETRIC=YCBCR",
            "TILED=YES",
        ]
        writer = QgsRasterFileWriter(out_path)
        writer.setCreateOptions(create_options_jpeg)
        error = writer.writeRaster(
            pipe, cols, rows, extent, raster_layer.crs(), transform_ctx,
        )

        if error == QgsRasterFileWriter.NoError and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path

        pipe2 = QgsRasterPipe()
        if not pipe2.set(provider.clone()):
            raise RuntimeError("无法创建栅格数据管道 (fallback)")

        create_options_deflate = ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"]
        writer2 = QgsRasterFileWriter(out_path)
        writer2.setCreateOptions(create_options_deflate)
        error2 = writer2.writeRaster(
            pipe2, cols, rows, extent, raster_layer.crs(), transform_ctx,
        )
        if error2 == QgsRasterFileWriter.NoError and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path

        raise RuntimeError(f"QgsRasterFileWriter 写入失败")

    except Exception as err:
        raise RuntimeError(f"图层裁剪失败: {str(err)}")