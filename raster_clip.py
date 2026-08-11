# -*- coding: utf-8 -*-
"""
原生栅格裁剪逻辑（从原 worker.py 中提取为独立函数）：
- 优先使用 GDAL 直读快速裁切（针对本地普通格式文件）
- 失败则回退到 QGIS 内置 QgsRasterFileWriter 原生管道写盘
  （100% 可用，不依赖 Processing 工具箱里的 gdal:cliprasterbyextent 算法）
"""

import os
import tempfile

from qgis.core import QgsRasterFileWriter, QgsRasterPipe, QgsProject


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
        proj_win = [
            extent.xMinimum(),
            extent.yMaximum(),
            extent.xMaximum(),
            extent.yMinimum(),
        ]
        creation_options = [
            "COMPRESS=DEFLATE",  # 无损压缩
            "PREDICTOR=2",  # 提升压缩率（对 8bit/16bit 极其有效）
            "TILED=YES",  # 分块存储
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


def _write_with_raster_file_writer(raster_layer, extent, out_path):
    """方案 2: 使用 QGIS 内置 QgsRasterFileWriter 原生 C++ 管道写盘"""
    try:
        provider = raster_layer.dataProvider()
        pipe = QgsRasterPipe()
        if not pipe.set(provider.clone()):
            raise RuntimeError("无法创建栅格数据管道")

        create_options = ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"]
        writer = QgsRasterFileWriter(out_path)
        writer.setCreateOptions(create_options)  # 设置压缩参数

        x_res = raster_layer.rasterUnitsPerPixelX()
        y_res = raster_layer.rasterUnitsPerPixelY()
        if x_res <= 0 or y_res <= 0:
            x_res = y_res = 1.0

        cols = max(1, int(round(extent.width() / x_res)))
        rows = max(1, int(round(extent.height() / y_res)))

        transform_ctx = QgsProject.instance().transformContext()
        error = writer.writeRaster(
            pipe,
            cols,
            rows,
            extent,
            raster_layer.crs(),
            transform_ctx,
        )

        if error == QgsRasterFileWriter.NoError and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path

        raise RuntimeError(f"QgsRasterFileWriter 写入失败，错误码: {error}")

    except Exception as err:
        raise RuntimeError(f"图层裁剪失败: {str(err)}")
