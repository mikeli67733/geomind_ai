# -*- coding: utf-8 -*-
"""
GeoMind AI Copilot – DeepSeek 版（增强调试）
"""
import os
import traceback
from qgis.core import QgsTask
from qgis.PyQt.QtCore import pyqtSignal

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class LlmApiTask(QgsTask):
    apiFinished = pyqtSignal(object)
    apiError = pyqtSignal(str)
    apiProgress = pyqtSignal(str)

    def __init__(self, payload: dict, api_url: str, api_key: str):
        super().__init__("LLM 网络通信中...", QgsTask.CanCancel)
        self.payload = payload
        self.api_url = (api_url or "https://api.deepseek.com").strip()
        self.api_key = (api_key or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
        self.response_msg = None
        self.error_msg = None

    def run(self) -> bool:
        try:
            self.apiProgress.emit("🔍 检查 openai 库...")
            if not OPENAI_AVAILABLE:
                self.error_msg = "未安装 openai 库！请在 QGIS 的 Python 环境执行：pip install openai"
                return False

            self.apiProgress.emit(f"🔑 API Key 状态: {'已配置' if self.api_key else '❌ 空！'}")
            if not self.api_key:
                self.error_msg = "DeepSeek API Key 为空！请到【设置】页填写，或设置环境变量 DEEPSEEK_API_KEY"
                return False

            self.apiProgress.emit(f"🔗 连接地址: {self.api_url}")

            client = OpenAI(
                api_key=self.api_key,
                base_url=self.api_url,
                timeout=60.0
            )

            model = self.payload.get("model", "deepseek-v4-pro")
            messages = self.payload.get("messages", [])
            tools = self.payload.get("tools")
            tool_choice = self.payload.get("tool_choice")

            # # 强制把模型改成 DeepSeek 真正支持的名称（防止用户填了不存在的 v4-flash）
            # if "v4" in model or "flash" in model:
            #     model = "deepseek-chat"
            #     self.apiProgress.emit(f"⚠️ 模型名已自动修正为: {model}")

            self.apiProgress.emit(f"📡 正在请求 DeepSeek（model={model}）...")

            kwargs = {
                "model": model,
                "messages": messages,
                "stream": False,
            }
            if tools is not None:
                kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice

            response = client.chat.completions.create(**kwargs)

            self.apiProgress.emit("✅ 收到响应，正在解析...")

            msg = response.choices[0].message
            self.response_msg = {
                "role": msg.role,
                "content": msg.content or "",
            }

            # 思考过程（如果有）
            if hasattr(msg, "reasoning_content") and msg.reasoning_content:
                self.response_msg["reasoning_content"] = msg.reasoning_content

            # 工具调用
            if getattr(msg, "tool_calls", None):
                self.response_msg["tool_calls"] = []
                for tc in msg.tool_calls:
                    self.response_msg["tool_calls"].append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    })

            return True

        except Exception as e:
            self.error_msg = f"DeepSeek 调用失败:\n{str(e)}\n\n{traceback.format_exc()}"
            return False

    def finished(self, result: bool):
        if result and self.response_msg is not None:
            self.apiFinished.emit(self.response_msg)
        else:
            self.apiError.emit(self.error_msg or "未知错误")