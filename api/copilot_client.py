# -*- coding: utf-8 -*-
"""
Copilot streaming client — bridges the QGIS frontend to the backend agent.

The backend now runs the whole agent loop (LLM + skill execution) inside a
single SSE request; this QThread just streams the events to the UI:

- reasoning / text          → thinking & answer deltas
- tool_call / tool_result   → skill progress cards
- action                    → frontend commands (load_layer / focus_map /
                              set_extent)
- task_progress             → AI task progress cards
- done / error              → terminal events
"""
import json
import os

from qgis.PyQt.QtCore import QThread, pyqtSignal

from .http_client import HttpClient
from ..core.constants import API_COPILOT_CHAT, API_COPILOT_UPLOAD
from ..core.logger import get_logger

logger = get_logger("api.copilot_client")


def upload_copilot_layer(
    server_url: str,
    token: str,
    file_path: str,
    layer_key: str,
    timeout: float = 180,
) -> str:
    """Upload a local data file to the backend copilot store.

    Used when the backend runs on a different machine than QGIS: the chat
    request only carries paths, so the referenced layer data must first be
    uploaded for server-side GDAL skills to open. Returns the server-side
    path to put into the layer descriptor.
    """
    from ..core.exceptions import ServerUnreachableError

    client = HttpClient(token=token, request_timeout=timeout, retries=1)
    try:
        with open(file_path, "rb") as fh:
            resp = client.post(
                f"{server_url.rstrip('/')}{API_COPILOT_UPLOAD}",
                files={"file": (os.path.basename(file_path), fh)},
                data={"layer_key": layer_key},
                auth=True,
                timeout=timeout,
            )
    except ServerUnreachableError as exc:
        raise RuntimeError(f"无法连接服务器上传图层: {exc}") from exc
    finally:
        client.close()

    if resp.status_code == 401:
        raise RuntimeError("登录已过期，请重新登录后再试")
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"图层上传失败 ({resp.status_code}): {detail}")
    return resp.json()["server_path"]


class BackendCopilotTask(QThread):
    """Streams copilot agent events from the backend via SSE."""

    chunkReceived = pyqtSignal(dict)
    taskFinished = pyqtSignal()
    taskError = pyqtSignal(str)

    def __init__(
        self,
        server_url: str,
        token: str,
        messages: list,
        active_layers: list,
        canvas_extent: dict = None,
        machine_id: str = "",
    ):
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.messages = messages
        self.active_layers = active_layers
        self.canvas_extent = canvas_extent
        self.machine_id = machine_id
        self._client = None
        self._resp = None
        self._cancel_requested = False

    def run(self) -> None:
        url = f"{self.server_url}{API_COPILOT_CHAT}"
        payload = {
            "messages": self.messages,
            "active_layers": self.active_layers,
            "canvas_extent": self.canvas_extent or {},
            "machine_id": self.machine_id or "",
        }

        try:
            self._client = HttpClient(token=self.token, request_timeout=60, retries=1)
            # retries=0: SSE streams must never be replayed after a partial read.
            # 连接 10s / 读取 300s：工具多轮往返可能耗时较长，避免流中途被掐断。
            self._resp = self._client.post(
                url, json=payload, auth=True, timeout=(10, 300), retries=0
            )
            if self._resp.status_code == 401:
                self.taskError.emit("登录已过期，请重新登录后再试")
                return
            if self._resp.status_code == 402:
                self.taskError.emit("今日免费额度已用完，请升级会员或联系作者")
                return
            if self._resp.status_code != 200:
                try:
                    err_detail = self._resp.json().get("detail", self._resp.text)
                except Exception:
                    err_detail = self._resp.text
                self.taskError.emit(f"服务异常 ({self._resp.status_code}): {err_detail}")
                return

            for line in self._resp.iter_lines(decode_unicode=True):
                if self._cancel_requested:
                    return
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

        except Exception as e:
            logger.error("Copilot connection failed: %s", e)
            self.taskError.emit(f"连接服务器失败: {e}")
            return
        finally:
            if self._resp:
                self._resp.close()
            if self._client:
                self._client.close()

        self.taskFinished.emit()

    def request_cancel(self):
        """Best-effort abort of the SSE stream."""
        self._cancel_requested = True
        if self._resp:
            try:
                self._resp.close()
            except Exception:
                pass
