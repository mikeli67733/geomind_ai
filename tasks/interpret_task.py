# -*- coding: utf-8 -*-
"""
Interpretation task — asynchronous QGIS task for cloud-based image interpretation.

Supports single-image (segmentation/detection) and dual-image (change detection)
workflows.  Uses QgsTask for proper background execution and cancellation.
"""
import os
import time
import tempfile
import traceback

from qgis.PyQt.QtCore import pyqtSignal
from qgis.core import QgsTask, QgsRectangle

from ..api.task_client import GeoMindApiClient
from ..core.exceptions import TaskCancelledError, GeoMindError, ExtentTooLargeError
from ..utils.extent_guard import check_extent_too_large
from ..core.constants import (
    DEFAULT_POLL_INTERVAL,
    CANCEL_CHECK_INTERVAL,
    DEFAULT_TIMEOUT,
    PLUGIN_TASK_DESCRIPTION,
)
from ..core.compat import TASK_CAN_CANCEL
from ..core.logger import get_logger

logger = get_logger("tasks.interpret")


class InterpretTask(QgsTask):
    """Async interpretation task that clips, submits, polls, and downloads."""

    progressMessage = pyqtSignal(str)
    taskSucceeded = pyqtSignal(str, str)  # (result_file_path, content_type)
    taskFailed = pyqtSignal(str)
    taskCancelled = pyqtSignal()

    def __init__(
        self,
        raster_layer,
        extent: QgsRectangle,
        extent_crs,
        model_key: str,
        target_class: str,
        prompt: str,
        server_url: str,
        output_format: str = "mask",
        license_key: str = "TEST_KEY",
        machine_id: str = "MACHINE_01",
        token: str = "",
        raster_layer_after=None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        super().__init__(PLUGIN_TASK_DESCRIPTION, TASK_CAN_CANCEL)
        self.raster_layer = raster_layer
        self.raster_layer_after = raster_layer_after
        self.extent = extent
        self.extent_crs = extent_crs
        self.model_key = model_key
        self.target_class = target_class
        self.prompt = prompt
        self.output_format = output_format
        self.server_url = server_url.rstrip("/")
        self.license_key = license_key
        self.machine_id = machine_id
        self.token = token
        self.poll_interval = poll_interval
        self.timeout = timeout

        self._client: GeoMindApiClient = None
        self._task_id: str = None
        self.result_path: str = None
        self.content_type: str = None
        self.error_message: str = None

    # -- Background thread --------------------------------------------------

    def run(self) -> bool:
        clipped_path = None
        clipped_path_after = None
        try:
            # Hard guard: reject oversized extents before any clipping/upload.
            too_large, guard_msg = check_extent_too_large(
                self.raster_layer, self.extent, self.extent_crs
            )
            if too_large:
                raise ExtentTooLargeError(guard_msg)

            self._client = GeoMindApiClient(
                self.server_url, self.license_key, self.machine_id, token=self.token
            )

            self._raise_if_cancelled()
            self.progressMessage.emit("正在裁剪所选范围的 T1 影像...")

            from ..utils.raster_clip import clip_raster_to_temp

            clipped_path = clip_raster_to_temp(
                self.raster_layer,
                self.extent,
                self.extent_crs,
                f"{os.getpid()}_{id(self)}_t1",
            )

            if self.raster_layer_after:
                self.progressMessage.emit("正在裁剪所选范围的 T2 变化期影像...")
                clipped_path_after = clip_raster_to_temp(
                    self.raster_layer_after,
                    self.extent,
                    self.extent_crs,
                    f"{os.getpid()}_{id(self)}_t2",
                )

            clip_size_mb = os.path.getsize(clipped_path) / (1024 * 1024)
            self.progressMessage.emit(
                f"影像裁剪完成 ({clip_size_mb:.1f} MB)，正在提交任务到云端网关..."
            )

            self._task_id = self._client.submit_task(
                clipped_path,
                self.model_key,
                self.target_class,
                self.prompt,
                image_after_path=clipped_path_after,
                output_format=self.output_format,
            )

            self._raise_if_cancelled(notify_server=True)
            self.progressMessage.emit(
                f"任务下发成功 (ID: {self._task_id})，正在等待排队解译..."
            )

            self.result_path, self.content_type = self._poll_until_done()
            return True

        except TaskCancelledError:
            self.error_message = "用户已取消任务"
            return False
        except Exception as e:
            self.error_message = f"{e}\n{traceback.format_exc()}"
            return False
        finally:
            for path in (clipped_path, clipped_path_after):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
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
                raise GeoMindError("解译超时：服务器未在规定时间内完成计算")

            info = self._client.get_status(self._task_id, model_key=self.model_key)
            status = info.get("status")
            message = info.get("message", "运行中...")
            self.progressMessage.emit(message)

            if status == "completed":
                self.progressMessage.emit("解译完成，正在下载结果图层...")
                content_type = info.get("content_type", "")
                suffix = (
                    ".geojson"
                    if ("geo+json" in content_type or "json" in content_type)
                    else ".tif"
                )
                local_result_path = os.path.join(
                    tempfile.gettempdir(), f"result_{self._task_id}{suffix}"
                )
                self._client.download_result(
                    self._task_id, local_result_path, model_key=self.model_key
                )
                return local_result_path, content_type

            elif status == "failed":
                error_msg = info.get("error_msg", "未知错误")
                raise GeoMindError(f"服务器端计算失败: {error_msg}")

            self._sleep_with_cancel_check(self.poll_interval)

    def _sleep_with_cancel_check(self, seconds: float):
        waited = 0.0
        while waited < seconds:
            if self.isCanceled():
                return
            step = min(CANCEL_CHECK_INTERVAL, seconds - waited)
            time.sleep(step)
            waited += step

    # -- Main thread callback ------------------------------------------------

    def finished(self, result: bool):
        if self.isCanceled():
            self.taskCancelled.emit()
        elif result:
            self.taskSucceeded.emit(self.result_path or "", self.content_type or "")
        else:
            self.taskFailed.emit(self.error_message or "未知错误")

    def cancel(self):
        self.progressMessage.emit("正在取消任务，请稍候...")
        super().cancel()
