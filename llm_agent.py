# -*- coding: utf-8 -*-
"""
llm_agent.py - QGIS 前端与后端通信 Task
"""
import json
import requests
from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal


class BackendCopilotTask(QgsTask):
    chunkReceived = pyqtSignal(dict)
    taskFinished = pyqtSignal()
    taskError = pyqtSignal(str)

    def __init__(self, server_url: str, token: str, messages: list, active_layers: list, machine_id: str = ""):
        super().__init__("GeoMind Copilot 通信中...", QgsTask.CanCancel)
        self.server_url = server_url.rstrip('/')
        self.token = token
        self.messages = messages
        self.active_layers = active_layers
        self.machine_id = machine_id  # 👈 声明并保存 machine_id
        self._resp = None

    def run(self) -> bool:
        url = f"{self.server_url}/api/v1/copilot/chat"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messages": self.messages,
            "active_layers": self.active_layers,
            "machine_id": self.machine_id or ""
        }

        try:
            self._resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=60)
            if self._resp.status_code == 401:
                self.taskError.emit("登录已过期，请重新登录后再试")
                return False
            if self._resp.status_code == 402:
                self.taskError.emit("今日免费额度已用完，请升级会员或联系作者")
                return False
            if self._resp.status_code != 200:
                try:
                    err_detail = self._resp.json().get('detail', self._resp.text)
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
            self.taskError.emit(f"连接服务器失败: {str(e)}")
            return False
        finally:
            if self._resp:
                self._resp.close()

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