# -*- coding: utf-8 -*-
"""
InterpretTask：基于 QGIS 原生任务框架 (QgsTask + QgsTaskManager) 实现的
异步解译任务，取代原先手写的 QThread 方案 (worker.py)。

更新特性：
- 完美支持单图解译 (分割/目标检测) 与双图解译 (变化检测)
- 支持 SAM3 输出类型切换 (mask 图斑 vs bbox 方框)
- 适配 raster_layer_after 参数输入，自动对 T2 影像进行 ROI 裁剪与打包提交
"""

import os
import time
import tempfile
import traceback

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask, QgsRectangle

from .api_client import GeoMindApiClient, TaskCancelledError
from .raster_clip import clip_raster_to_temp
from .constants import (
    DEFAULT_POLL_INTERVAL,
    CANCEL_CHECK_INTERVAL,
    DEFAULT_TIMEOUT,
    PLUGIN_TASK_DESCRIPTION,
)

# PyQt5 / PyQt6 及不同 QGIS 版本下 QgsTask.Flag 枚举写法兼容处理
TASK_CAN_CANCEL = getattr(QgsTask, 'CanCancel', None)
if TASK_CAN_CANCEL is None:
    TASK_CAN_CANCEL = QgsTask.Flag.CanCancel


class InterpretTask(QgsTask):

    # 状态文本更新，转发给 dockwidget 显示在状态栏
    progressMessage = pyqtSignal(str)
    # (result_file_path, content_type)
    taskSucceeded = pyqtSignal(str, str)
    # error_message
    taskFailed = pyqtSignal(str)
    # 用户（或 QGIS）主动取消
    taskCancelled = pyqtSignal()

    def __init__(self, raster_layer, extent: QgsRectangle, extent_crs, model_key: str,
                 target_class: str, prompt: str, server_url: str,
                 output_format: str = "mask",  # 👈 新增：输出类型参数 ('mask' 或 'bbox')
                 license_key: str = "TEST_KEY", machine_id: str = "MACHINE_01",
                 token: str = "", raster_layer_after=None,
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 timeout: int = DEFAULT_TIMEOUT):
        super().__init__(PLUGIN_TASK_DESCRIPTION, TASK_CAN_CANCEL)
        self.raster_layer = raster_layer
        self.raster_layer_after = raster_layer_after
        self.extent = extent
        self.extent_crs = extent_crs  # 保存画布坐标系
        self.model_key = model_key
        self.target_class = target_class
        self.prompt = prompt
        self.output_format = output_format  # 👈 存储输出类型
        self.server_url = server_url.rstrip('/')
        self.license_key = license_key
        self.machine_id = machine_id
        self.token = token
        self.poll_interval = poll_interval
        self.timeout = timeout

        self._client = None
        self._task_id = None

        # run() 在工作线程中写入，finished() 在主线程中读取
        self.result_path = None
        self.content_type = None
        self.error_message = None

    # -------------------------------------------------------- 工作线程 ----
    def run(self) -> bool:
        clipped_path = None
        clipped_path_after = None
        try:
            self._client = GeoMindApiClient(
                self.server_url, self.license_key, self.machine_id, token=self.token
            )

            self._raise_if_cancelled()
            self.progressMessage.emit("正在裁剪所选范围的 T1 影像...")

            # 传入 self.extent_crs
            clipped_path = clip_raster_to_temp(
                self.raster_layer, self.extent, self.extent_crs, f"{os.getpid()}_{id(self)}_t1"
            )

            if self.raster_layer_after:
                self.progressMessage.emit("正在裁剪所选范围的 T2 变化期影像...")
                clipped_path_after = clip_raster_to_temp(
                    self.raster_layer_after, self.extent, self.extent_crs, f"{os.getpid()}_{id(self)}_t2"
                )

            # 计算图像大小提示
            clip_size_mb = os.path.getsize(clipped_path) / (1024 * 1024)
            self.progressMessage.emit(
                f"影像裁剪完成 ({clip_size_mb:.1f} MB)，正在提交任务到云端网关..."
            )

            # 🚨【关键修改】：透传 output_format 给 client.submit_task
            self._task_id = self._client.submit_task(
                clipped_path,
                self.model_key,
                self.target_class,
                self.prompt,
                image_after_path=clipped_path_after,  # 双图路径传入
                output_format=self.output_format       # 👈 传入输出格式 ('mask'/'bbox')
            )

            self._raise_if_cancelled(notify_server=True)
            self.progressMessage.emit(f"任务下发成功 (ID: {self._task_id})，正在等待排队解译...")

            self.result_path, self.content_type = self._poll_until_done()
            return True

        except TaskCancelledError:
            self.error_message = "用户已取消任务"
            return False
        except Exception as e:
            self.error_message = f"{e}\n{traceback.format_exc()}"
            return False
        finally:
            # 清理临时裁剪文件
            if clipped_path and os.path.exists(clipped_path):
                try: os.remove(clipped_path)
                except OSError: pass
            if clipped_path_after and os.path.exists(clipped_path_after):
                try: os.remove(clipped_path_after)
                except OSError: pass
            if self._client:
                self._client.close()

    def _raise_if_cancelled(self, notify_server: bool = False):
        if not self.isCanceled():
            return
        if notify_server and self._task_id and self._client:
            self._client.cancel_task(self._task_id)
        raise TaskCancelledError()

    def _poll_until_done(self):
        start_time = time.time()

        while True:
            self._raise_if_cancelled(notify_server=True)

            if time.time() - start_time > self.timeout:
                raise RuntimeError("解译超时：服务器未在规定时间内完成计算")

            info = self._client.get_status(self._task_id, model_key=self.model_key)
            status = info.get("status")
            message = info.get("message", "运行中...")
            self.progressMessage.emit(f"⏳ [云端进度]: {message}")

            if status == "completed":
                self.progressMessage.emit("解译完成，正在下载结果图层...")
                content_type = info.get("content_type", "")
                suffix = ".geojson" if ("geo+json" in content_type or "json" in content_type) else ".tif"
                local_result_path = os.path.join(tempfile.gettempdir(), f"result_{self._task_id}{suffix}")
                self._client.download_result(self._task_id, local_result_path, model_key=self.model_key)
                return local_result_path, content_type

            elif status == "failed":
                error_msg = info.get("error_msg", "未知错误")
                raise RuntimeError(f"服务器端计算失败: {error_msg}")

            self._sleep_with_cancel_check(self.poll_interval)

    def _sleep_with_cancel_check(self, seconds: float):
        waited = 0.0
        while waited < seconds:
            if self.isCanceled():
                return
            step = min(CANCEL_CHECK_INTERVAL, seconds - waited)
            time.sleep(step)
            waited += step

    # ---------------------------------------------------------- 主线程 ----
    def finished(self, result: bool):
        """
        QgsTaskManager 保证 finished() 总是在主线程被回调。
        """
        if self.isCanceled():
            self.taskCancelled.emit()
        elif result:
            self.taskSucceeded.emit(self.result_path or "", self.content_type or "")
        else:
            self.taskFailed.emit(self.error_message or "未知错误")

    def cancel(self):
        """用户点击取消 / QGIS 任务面板取消时触发。"""
        self.progressMessage.emit("正在取消任务，请稍候...")
        super().cancel()