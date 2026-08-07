# -*- coding: utf-8 -*-
"""
InterpretWorker 异步线程：负责将图像与参数传递给服务器
- 使用 QgsRasterFileWriter 与 GDAL 原生 API 实现线程安全裁剪
"""
from datetime import datetime  # <--- 1. 在文件最开头添加导入
import os
import tempfile
import traceback

from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import (
    QgsRectangle, QgsRasterPipe, QgsRasterFileWriter, QgsProject
)


class InterpretWorker(QThread):

    finished_ok = pyqtSignal(str, str)     # (result_file_path, content_type)
    finished_error = pyqtSignal(str)       # error_message
    progress = pyqtSignal(str)             # status text

    def __init__(self, raster_layer, extent: QgsRectangle, model_key: str,
                 target_class: str, prompt: str, server_url: str, 
                 license_key: str = "", machine_id: str = "", # <--- 新增参数
                 timeout: int = 600, parent=None):
        super().__init__(parent)
        self.raster_layer = raster_layer
        self.extent = extent
        self.model_key = model_key
        self.target_class = target_class
        self.prompt = prompt
        self.server_url = server_url.rstrip('/')
        self.license_key = license_key   # <--- 新增保存
        self.machine_id = machine_id     # <--- 新增保存
        self.timeout = timeout

    def run(self):
        clipped_path = None
        try:
            self.progress.emit("正在裁剪所选范围的影像...")
            clipped_path = self._clip_raster()

            self.progress.emit("正在向远程服务器下发解译任务...")
            result_path, content_type = self._call_server(clipped_path)

            self.finished_ok.emit(result_path, content_type)

        except Exception as e:
            self.finished_error.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            if clipped_path and os.path.exists(clipped_path):
                try:
                    os.remove(clipped_path)
                except OSError:
                    pass

    def _clip_raster(self) -> str:
        """安全地将栅格图层按选中范围裁剪输出临时 GeoTIFF 文件"""
        out_path = os.path.join(tempfile.gettempdir(), f"input_clip_{os.getpid()}_{id(self)}.tif")
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass

        # 检查范围合法性
        if self.extent.isEmpty() or self.extent.width() <= 0 or self.extent.height() <= 0:
            raise RuntimeError("选区范围无效或宽高为 0，请重新框选范围")

        # -------------------------------------------------------------
        # 方案 1: 优先使用 GDAL Python API（最快，适合本地标准栅格文件）
        # -------------------------------------------------------------
        src_path = self.raster_layer.source()
        try:
            from osgeo import gdal
            gdal.UseExceptions()

            # 尝试通过 GDAL 打开数据集
            ds = gdal.Open(src_path)
            if ds is None:
                src_path = self.raster_layer.dataProvider().dataSourceUri()
                ds = gdal.Open(src_path)

            if ds is not None:
                # GDAL projWin 顺序: [ulx, uly, lrx, lry] -> [xmin, ymax, xmax, ymin]
                proj_win = [
                    self.extent.xMinimum(),
                    self.extent.yMaximum(),
                    self.extent.xMaximum(),
                    self.extent.yMinimum()
                ]
                options = gdal.TranslateOptions(projWin=proj_win, format='GTiff')
                out_ds = gdal.Translate(out_path, ds, options=options)
                out_ds = None  # 刷盘并释放
                ds = None

                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path
        except Exception as e:
            print(f"[GDAL Direct Direct Warning]: {e}, 回退到 QgsRasterFileWriter 方式裁剪...")

        # -------------------------------------------------------------
        # 方案 2: 使用 QGIS 原生 C++ API (QgsRasterFileWriter)
        # 优点: 线程安全，100% 兼容 WMS、Memory图层及各类特有格式
        # -------------------------------------------------------------
        try:
            pipe = QgsRasterPipe()
            provider = self.raster_layer.dataProvider()
            
            if pipe.set(provider.clone()):
                writer = QgsRasterFileWriter(out_path)
                writer.setOutputFormat("GTiff")

                # 计算像素行列数
                x_res = self.raster_layer.rasterUnitsPerPixelX()
                y_res = self.raster_layer.rasterUnitsPerPixelY()
                if x_res <= 0 or y_res <= 0:
                    x_res = 1.0
                    y_res = 1.0

                n_cols = max(1, int(round(self.extent.width() / x_res)))
                n_rows = max(1, int(round(self.extent.height() / y_res)))

                err = writer.writeRaster(
                    pipe,
                    n_cols,
                    n_rows,
                    self.extent,
                    self.raster_layer.crs(),
                    QgsProject.instance().transformContext()
                )

                if err == QgsRasterFileWriter.NoError and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path
                else:
                    raise RuntimeError(f"QgsRasterFileWriter 导出失败，错误码: {err}")
        except Exception as e:
            raise RuntimeError(f"栅格裁剪彻底失败: {e}")

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("影像裁剪失败，未生成临时文件，请检查图层源或选区范围")

        return out_path

    def _call_server(self, image_path: str):
        """调用 HTTP POST 服务上传图片与参数"""
        try:
            import requests
        except ImportError:
            raise RuntimeError("Python 环境缺少 requests 模块，请执行 pip install requests 后重启 QGIS")

        url = f"{self.server_url}/interpret"

        # 组装请求参数
        data = {
            'model_key': self.model_key,
            'license_key': self.license_key,  # <--- 新增传递卡密
            'machine_id': self.machine_id  # <--- 新增传递机器码
        }
        if self.target_class:
            data['target_class'] = self.target_class
        if self.prompt:
            data['prompt'] = self.prompt

        with open(image_path, 'rb') as f:
            files = {'image': (os.path.basename(image_path), f, 'image/tiff')}
            resp = requests.post(url, files=files, data=data, timeout=self.timeout)

        if resp.status_code != 200:
            try:
                err = resp.json().get('error', resp.text)
            except Exception:
                err = resp.text
            raise RuntimeError(f"服务器返回异常 ({resp.status_code}): {err}")

        content_type = resp.headers.get('Content-Type', '').lower()

        # 判断返回文件后缀名
        if 'tif' in content_type or 'tiff' in content_type:
            suffix = '.tif'
        elif 'geo+json' in content_type or 'json' in content_type:
            suffix = '.geojson'
        elif 'zip' in content_type or 'shapefile' in content_type:
            suffix = '.zip'
        else:
            suffix = '.tif' if resp.content[:4] in (b'II*\x00', b'MM\x00*') else '.geojson'

        time_str = datetime.now().strftime("%Y%m%d_%H%M%S")

        result_path = os.path.join(
            tempfile.gettempdir(),
            f"result_{self.model_key}_{time_str}{suffix}"  # <--- 2. 改用 time_str
        )
        with open(result_path, 'wb') as f:
            f.write(resp.content)

        return result_path, content_type