# -*- coding: utf-8 -*-
"""
原生栅格裁剪逻辑（从原 worker.py 中提取为独立函数）：
- 优先使用 GDAL 直读快速裁切（针对本地普通格式文件），输出 JPEG 压缩 GeoTIFF
- 失败则回退到 QGIS 内置 QgsRasterFileWriter 原生管道写盘
  （100% 可用，不依赖 Processing 工具箱里的 gdal:cliprasterbyextent 算法）

压缩策略：
- 默认使用 JPEG 压缩（quality=85），对遥感影像体积可缩小 5-15 倍，
  AI 推理质量几乎无损；仍为 .tif 格式，带完整地理坐标，服务端无需改动
- 若数据为 16-bit，自动转为 8-bit（JPEG 仅支持 8-bit）
- JPEG 压缩失败时自动回退到 DEFLATE 无损压缩
"""

import os
import tempfile

from qgis.core import QgsRasterFileWriter, QgsRasterPipe, QgsProject

# JPEG 压缩质量（85 兼顾体积与质量，可根据需要调整 75-95）
JPEG_QUALITY = 85


def clip_raster_to_temp(raster_layer, extent, unique_suffix: str) -> str:
    """
    将 raster_layer 中 extent 范围内的像元裁剪到一个临时 TIFF 文件，返回文件路径。
    调用方需要负责在使用完毕后删除该临时文件。
    """
    out_path = os.path.join(tempfile.gettempdir(), f"input_clip_{unique_suffix}.tif")
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass

    gdal_result = _try_gdal_translate(raster_layer, extent, out_path)
    if gdal_result:
        return gdal_result

    return _write_with_raster_file_writer(raster_layer, extent, out_path)


def _try_gdal_translate(raster_layer, extent, out_path):
    """方案 1: 清理 URI 并使用 GDAL 直读快速裁切（针对本地普通格式文件）"""
    src_path = raster_layer.source()
    if src_path.startswith("file:///"):
        src_path = src_path[8:]
    src_path = src_path.split("|")[0].split("?")[0].strip('"\' ')

    if not os.path.exists(src_path):
        return None

    try:
        from osgeo import gdal

        # 先探测源影像的波段数和数据类型，决定压缩策略
        band_count, is_16bit = _probe_src_info(src_path)

        proj_win = [
            extent.xMinimum(),
            extent.yMaximum(),
            extent.xMaximum(),
            extent.yMinimum(),
        ]

        # ---- 尝试 JPEG 压缩（体积大幅缩小 5-15 倍）----
        jpeg_result = _try_jpeg_compress(
            src_path, out_path, proj_win, band_count, is_16bit
        )
        if jpeg_result:
            return jpeg_result

        # ---- JPEG 不可用，回退 DEFLATE 无损压缩 ----
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
    """探测源影像的波段数和是否为 16-bit 数据类型"""
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
    """
    使用 JPEG 压缩输出 GeoTIFF。
    - 3 波段 RGB → PHOTOMETRIC=YCBCR（压缩率最高）
    - 1 波段灰度 → PHOTOMETRIC=MINISBLACK
    - 16-bit 数据先转 8-bit（JPEG 仅支持 8-bit）
    - 其他波段数跳过 JPEG，交由调用方回退 DEFLATE
    """
    try:
        from osgeo import gdal

        if band_count >= 3:
            photometric = "YCBCR"
        elif band_count == 1:
            photometric = "MINISBLACK"
        else:
            return None  # 2 波段 / 4+ 波段 JPEG 兼容性差，跳过

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
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(f"[raster_clip] JPEG 压缩完成，文件大小: {size_mb:.1f} MB")
            return out_path
    except Exception as err:
        print(f"JPEG compress failed, will fallback to DEFLATE: {err}")

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

        # 优先 JPEG 压缩
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
            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(f"[raster_clip] JPEG 压缩完成 (QGIS Writer)，文件大小: {size_mb:.1f} MB")
            return out_path

        # JPEG 失败 → 新建管道回退 DEFLATE
        print(f"[raster_clip] JPEG 写入失败 (code={error})，回退 DEFLATE...")
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

        raise RuntimeError(f"QgsRasterFileWriter 写入失败，JPEG error={error}, DEFLATE error={error2}")

    except Exception as err:
        raise RuntimeError(f"图层裁剪失败: {str(err)}")
