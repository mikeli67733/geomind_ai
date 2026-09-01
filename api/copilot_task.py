# -*- coding: utf-8 -*-
"""
Copilot streaming task — bridges the QGIS frontend to the backend LLM.

Uses QGIS QgsTask for background execution and streams SSE chunks back
to the UI via Qt signals.
"""
import json

from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

from .http_client import HttpClient
from ..core.constants import API_COPILOT_CHAT
from ..core.logger import get_logger

logger = get_logger("api.copilot_task")


class BackendCopilotTask(QgsTask):
    """Streams copilot chat responses from the backend via SSE."""

    chunkReceived = pyqtSignal(dict)
    taskFinished = pyqtSignal()
    taskError = pyqtSignal(str)

    def __init__(
        self,
        server_url: str,
        token: str,
        messages: list,
        active_layers: list,
        machine_id: str = "",
    ):
        super().__init__("GeoMind Copilot 通信中...", QgsTask.CanCancel)
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.messages = messages
        self.active_layers = active_layers
        self.machine_id = machine_id
        self._client = None
        self._resp = None

    def run(self) -> bool:
        url = f"{self.server_url}{API_COPILOT_CHAT}"
        payload = {
            "messages": self.messages,
            "active_layers": self.active_layers,
            "machine_id": self.machine_id or "",
        }

        try:
            self._client = HttpClient(token=self.token, request_timeout=180, retries=1)
            # retries=0: SSE streams must never be replayed after a partial read.
            self._resp = self._client.post(
                url, json=payload, auth=True, timeout=180, retries=0
            )
            if self._resp.status_code == 401:
                self.taskError.emit("登录已过期，请重新登录后再试")
                return False
            if self._resp.status_code == 402:
                self.taskError.emit("今日免费额度已用完，请升级会员或联系作者")
                return False
            if self._resp.status_code != 200:
                try:
                    err_detail = self._resp.json().get("detail", self._resp.text)
                except Exception:
                    err_detail = self._resp.text
                self.taskError.emit(f"服务异常 ({self._resp.status_code}): {err_detail}")
                return False

            for line in self._resp.iter_lines(decode_unicode=True):
                if self.isCanceled():
                    return False
                if not line or not line.startswith("data: "):
                    continue

                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                    self.chunkReceived.emit(data)
                except Exception:
                    continue

            return True

        except Exception as e:
            logger.error("Copilot connection failed: %s", e)
            self.taskError.emit(f"连接服务器失败: {e}")
            return False
        finally:
            if self._resp:
                self._resp.close()
            if self._client:
                self._client.close()

    def finished(self, result: bool):
        if result:
            self.taskFinished.emit()

    def cancel(self):
        super().cancel()
        if self._resp:
            try:
                self._resp.close()
            except Exception:
                pass
