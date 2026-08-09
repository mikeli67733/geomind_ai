# -*- coding: utf-8 -*-
"""
InterpretWorker 异步轮询线程：
- 使用 QgsRasterFileWriter 原生管道裁切，彻底解决 `gdal:cliprasterbyextent` 找不到算法的问题
- 支持异步提交任务与定时轮询获取状态
"""

import os
import time
import tempfile
import traceback

from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import QgsRectangle, QgsRasterFileWriter, QgsRasterPipe, QgsProject


class InterpretWorker(QThread):

    finished_ok = pyqtSignal(str, str)     # (result_file_path, content_type)
    finished_error = pyqtSignal(str)       # error_message
    progress = pyqtSignal(str)             # status text

    def __init__(self, raster_layer, extent: QgsRectangle, model_key: str,
                 target_class: str, prompt: str, server_url: str, 
                 license_key: str = "TEST_KEY", machine_id: str = "MACHINE_01",
                 poll_interval: float = 2.0, timeout: int = 600, parent=None):
        super().__init__(parent)
        self.raster_layer = raster_layer
        self.extent = extent
        self.model_key = model_key
        self.target_class = target_class
        self.prompt = prompt
        self.server_url = server_url.rstrip('/')
        self.license_key = license_key
        self.machine_id = machine_id
        self.poll_interval = poll_interval
        self.timeout = timeout

    def run(self):
        clipped_path = None
        try:
            self.progress.emit("正在裁剪所选范围的影像...")
            clipped_path = self._clip_raster()

            self.progress.emit("正在提交任务到云端服务器...")
            task_id = self._submit_task(clipped_path)

            self.progress.emit(f"任务下发成功 (ID: {task_id})，正在等待排队解译...")
            
            # 轮询获取任务状态
            result_path, content_type = self._poll_task_result(task_id)

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
        """原生安全的局部 TIFF 裁剪逻辑"""
        out_path = os.path.join(tempfile.gettempdir(), f"input_clip_{os.getpid()}_{id(self)}.tif")
        if os.path.exists(out_path):
            try: os.remove(out_path)
            except OSError: pass

        # 方案 1: 清理 URI 并使用 GDAL 直读快速裁切（针对本地普通格式文件）
        src_path = self.raster_layer.source()
        if src_path.startswith("file:///"):
            src_path = src_path[8:]
        src_path = src_path.split("|")[0].split("?")[0].strip('"\' ')

        if os.path.exists(src_path):
            try:
                from osgeo import gdal
                proj_win = [
                    self.extent.xMinimum(),
                    self.extent.yMaximum(),
                    self.extent.xMaximum(),
                    self.extent.yMinimum()
                ]
                options = gdal.TranslateOptions(projWin=proj_win)
                ds = gdal.Translate(out_path, src_path, options=options)
                ds = None
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return out_path
            except Exception as err:
                print(f"GDAL Direct Translate Failed: {err}")

        # 方案 2: 使用 QGIS 内置 QgsRasterFileWriter 原生 C++ 管道写盘 (100% 成功，无需依赖 Processing 工具箱)
        try:
            provider = self.raster_layer.dataProvider()
            pipe = QgsRasterPipe()
            if not pipe.set(provider.clone()):
                raise RuntimeError("无法创建栅格数据管道")

            writer = QgsRasterFileWriter(out_path)
            
            # 计算像素行列数
            x_res = self.raster_layer.rasterUnitsPerPixelX()
            y_res = self.raster_layer.rasterUnitsPerPixelY()
            if x_res <= 0 or y_res <= 0:
                x_res = y_res = 1.0

            cols = max(1, int(round(self.extent.width() / x_res)))
            rows = max(1, int(round(self.extent.height() / y_res)))

            transform_ctx = QgsProject.instance().transformContext()
            error = writer.writeRaster(
                pipe,
                cols,
                rows,
                self.extent,
                self.raster_layer.crs(),
                transform_ctx
            )

            if error == QgsRasterFileWriter.NoError and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            else:
                raise RuntimeError(f"QgsRasterFileWriter 写入失败，错误码: {error}")

        except Exception as err:
            raise RuntimeError(f"图层裁剪失败: {str(err)}")

    def _submit_task(self, image_path: str) -> str:
        """【1. 提交任务】获取 task_id"""
        try:
            import requests
        except ImportError:
            raise RuntimeError("缺少 requests 库，请执行 pip install requests")

        url = f"{self.server_url}/api/v1/task/submit"
        data = {
            'model_key': self.model_key,
            'license_key': self.license_key,
            'machine_id': self.machine_id
        }
        if self.target_class: data['target_class'] = self.target_class
        if self.prompt: data['prompt'] = self.prompt

        with open(image_path, 'rb') as f:
            files = {'image': (os.path.basename(image_path), f, 'image/tiff')}
            resp = requests.post(url, files=files, data=data, timeout=300)

        if resp.status_code != 200:
            try: err = resp.json().get('detail', resp.text)
            except Exception: err = resp.text
            raise RuntimeError(f"提交任务失败 ({resp.status_code}): {err}")

        return resp.json()["task_id"]

    def _poll_task_result(self, task_id: str):
        """【2. 定时轮询状态】直至成功或失败"""
        import requests

        status_url = f"{self.server_url}/api/v1/task/status/{task_id}"
        result_url = f"{self.server_url}/api/v1/task/result/{task_id}"

        start_time = time.time()

        while not self.isInterruptionRequested():
            if time.time() - start_time > self.timeout:
                raise RuntimeError("解译超时：服务器未在规定时间内完成计算")

            resp = requests.get(status_url, timeout=10)
            if resp.status_code == 200:
                info = resp.json()
                status = info.get("status")
                message = info.get("message", "运行中...")

                # 将服务器的实时状态输出到 QGIS 插件状态栏
                self.progress.emit(f"⏳ [云端进度]: {message}")

                if status == "completed":
                    # 【3. 下载结果】
                    self.progress.emit("解译完成，正在下载结果图层...")
                    res_resp = requests.get(result_url, timeout=60)
                    if res_resp.status_code != 200:
                        raise RuntimeError("下载结果文件失败")

                    content_type = info.get("content_type", "")
                    suffix = ".geojson" if "geo+json" in content_type or "json" in content_type else ".tif"
                    local_result_path = os.path.join(tempfile.gettempdir(), f"result_{task_id}{suffix}")

                    with open(local_result_path, 'wb') as f:
                        f.write(res_resp.content)

                    return local_result_path, content_type

                elif status == "failed":
                    error_msg = info.get("error_msg", "未知错误")
                    raise RuntimeError(f"服务器端计算失败: {error_msg}")

            time.sleep(self.poll_interval)

        raise RuntimeError("用户中断了操作")
