# -*- coding: utf-8 -*-
"""
InterpretTask：基于 QGIS 原生任务框架 (QgsTask + QgsTaskManager) 实现的
异步解译任务，取代原先手写的 QThread 方案 (worker.py)。

相比 QThread 自己维护线程 + 信号，使用 QgsTask 的收益：
- 自动出现在 QGIS 状态栏「后台任务」面板与任务管理器里，用户无需依赖
  插件自绘的进度条也能看到进度、取消任务
- QgsTaskManager 在 QGIS 退出 / 插件卸载时会自动请求取消所有任务，
  避免出现线程未回收就被强制退出导致崩溃的问题
- 与 Processing、其它核心/第三方插件的后台任务共用同一套原生机制，
  行为对用户来说是一致的、符合预期的

打断（取消）机制：
- 在裁剪前 / 提交前 / 提交后 / 每一次轮询前后共 4 个“安全点”检查
  self.isCanceled()，检测到取消立即抛出 TaskCancelledError 终止 run()
- 提交成功之后如果检测到取消，会尽力调用服务端“取消任务”接口，
  让服务端也尽早释放算力资源（服务端未实现该接口时静默忽略，不影响本地行为）
- 轮询等待被拆分为 0.3s 粒度的小睡眠，取消响应延迟不超过约 0.3 秒
- 触发方式：dockwidget 上的「取消任务」按钮，或 QGIS 原生任务面板里的取消按钮，
  两者最终都会调用同一个 QgsTask.cancel()
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

    def __init__(self, raster_layer, extent: QgsRectangle, model_key: str,
                 target_class: str, prompt: str, server_url: str,
                 license_key: str = "TEST_KEY", machine_id: str = "MACHINE_01",
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 timeout: int = DEFAULT_TIMEOUT):
        super().__init__(PLUGIN_TASK_DESCRIPTION, TASK_CAN_CANCEL)

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

        self._client = None
        self._task_id = None

        # run() 在工作线程中写入，finished() 在主线程中读取
        self.result_path = None
        self.content_type = None
        self.error_message = None

    # -------------------------------------------------------- 工作线程 ----
    def run(self) -> bool:
        """
        在 QGIS 任务线程池的某个工作线程中执行，禁止在此直接操作任何 UI 控件。
        返回 True 表示成功；返回 False 并结合 self.isCanceled() /
        self.error_message 区分是“失败”还是“被取消”。
        """
        clipped_path = None
        try:
            self._client = GeoMindApiClient(self.server_url, self.license_key, self.machine_id)

            self._raise_if_cancelled()
            self.progressMessage.emit("正在裁剪所选范围的影像...")
            clipped_path = clip_raster_to_temp(
                self.raster_layer, self.extent, f"{os.getpid()}_{id(self)}"
            )

            self._raise_if_cancelled()
            self.progressMessage.emit("正在提交任务到云端服务器...")
            self._task_id = self._client.submit_task(
                clipped_path, self.model_key, self.target_class, self.prompt
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
            if clipped_path and os.path.exists(clipped_path):
                try:
                    os.remove(clipped_path)
                except OSError:
                    pass
            if self._client:
                self._client.close()

    def _raise_if_cancelled(self, notify_server: bool = False):
        if not self.isCanceled():
            return
        if notify_server and self._task_id and self._client:
            # 任务已下发到服务端，尽力通知服务端一并停止计算（失败也无所谓）
            self._client.cancel_task(self._task_id)
        raise TaskCancelledError()

    def _poll_until_done(self):
        start_time = time.time()

        while True:
            self._raise_if_cancelled(notify_server=True)

            if time.time() - start_time > self.timeout:
                raise RuntimeError("解译超时：服务器未在规定时间内完成计算")

            info = self._client.get_status(self._task_id)
            status = info.get("status")
            message = info.get("message", "运行中...")
            self.progressMessage.emit(f"⏳ [云端进度]: {message}")

            if status == "completed":
                self.progressMessage.emit("解译完成，正在下载结果图层...")
                content_type = info.get("content_type", "")
                suffix = ".geojson" if ("geo+json" in content_type or "json" in content_type) else ".tif"
                local_result_path = os.path.join(tempfile.gettempdir(), f"result_{self._task_id}{suffix}")
                self._client.download_result(self._task_id, local_result_path)
                return local_result_path, content_type

            elif status == "failed":
                error_msg = info.get("error_msg", "未知错误")
                raise RuntimeError(f"服务器端计算失败: {error_msg}")

            # 把一次 poll_interval 拆成小步睡眠，取消响应更灵敏
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
        QgsTaskManager 保证 finished() 总是在主线程被回调（无论 run() 是
        正常结束、抛异常、还是被取消），因此这里发信号驱动 UI 是安全的。
        """
        if self.isCanceled():
            self.taskCancelled.emit()
        elif result:
            self.taskSucceeded.emit(self.result_path or "", self.content_type or "")
        else:
            self.taskFailed.emit(self.error_message or "未知错误")

    def cancel(self):
        """用户点击取消 / QGIS 任务面板取消时触发，实际中断逻辑见 run() 中的检查点。"""
        self.progressMessage.emit("正在取消任务，请稍候...")
        super().cancel()
