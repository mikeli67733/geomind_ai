# -*- coding: utf-8 -*-
"""同步版后端 LLM 调用与 Copilot Agent 循环封装。"""
import json
import logging
from typing import List, Dict, Any, Optional, Tuple

from ..api.http_client import HttpClient
from .config import settings
from .constants import (
    API_COPILOT_CHAT, SETTINGS_KEY_TOKEN, SETTINGS_ORG, SETTINGS_APP,
)
from .logger import get_logger

logger = get_logger("core.llm_client")

# 尝试导入插件内部现有的工具执行器；缺失时回退到技能白名单执行器
# （与前台 Copilot 的 _execute_local_skill 同一套：tools.skill_registry）
def _resolve_whitelist_executor():
    """返回按白名单执行工具的 callable；技能包延迟导入避免启动开销。"""
    try:
        from ..tools import skill_dispatcher  # noqa: F401  导入即注册白名单
        from ..tools.skill_registry import get_skill
    except Exception:
        return None

    def _run(fn_name: str, args: dict) -> str:
        try:
            spec = get_skill(fn_name)
            if spec is None:
                return f"未找到可执行工具或未在白名单注册: {fn_name}"
            return str(spec.func(**args))
        except Exception as exc:
            return f"工具执行失败: {exc}"

    return _run


try:
    from ..copilot.tool_runner import execute_tool
except Exception:
    try:
        from ..copilot.tools import execute_tool
    except Exception:
        execute_tool = _resolve_whitelist_executor()


def _get_token() -> str:
    try:
        from qgis.PyQt.QtCore import QSettings
        return str(QSettings(SETTINGS_ORG, SETTINGS_APP).value(SETTINGS_KEY_TOKEN, "")).strip()
    except Exception:
        return ""


def run_copilot_agent(
    prompt: str,
    active_layers: List[str],
    max_turns: int = 8,
    timeout: float = 60
) -> Tuple[Optional[str], str]:
    """驱动 Copilot Agent 完成工具调用闭环，返回 (final_summary, error_reason)"""
    token = _get_token()
    if not token:
        return None, "未登录，请先在插件中登录账号"

    try:
        server_url = settings.server_url().rstrip("/")
    except Exception as exc:
        return None, f"获取服务器地址失败: {exc}"

    messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
    client = HttpClient(token=token, request_timeout=timeout, retries=0)

    try:
        for turn in range(max_turns):
            payload = {
                "messages": messages,
                "active_layers": active_layers,
                "tools_enabled": True
            }
            resp = client.post(
                f"{server_url}{API_COPILOT_CHAT}",
                json=payload,
                auth=True,
                timeout=(10, timeout),
                retries=0,
                stream=True
            )
            if resp.status_code != 200:
                return None, f"后端请求失败 HTTP {resp.status_code}"

            text_parts = []
            tool_calls = []

            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except Exception:
                    continue

                if data.get("type") == "text" and data.get("content"):
                    text_parts.append(data["content"])
                elif data.get("type") == "tool_call" and data.get("tool_calls"):
                    tool_calls.extend(data["tool_calls"])
                elif data.get("type") == "error":
                    return None, f"模型服务报错: {data.get('content')}"

            resp.close()
            final_text = "".join(text_parts).strip()

            # 1. 如果大模型没有发起任何工具调用，说明已得出最终研判结论
            if not tool_calls:
                return final_text or "已完成分析处理。", ""

            # 2. 将 Assistant 的动作加入历史上下文
            assistant_msg: Dict[str, Any] = {"role": "assistant"}
            if final_text:
                assistant_msg["content"] = final_text
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # 3. 本地执行每个 Tool Call，并将执行结果反馈给模型
            for tc in tool_calls:
                tc_id = tc.get("id")
                func = tc.get("function", {})
                f_name = func.get("name")
                f_args_raw = func.get("arguments", "{}")
                try:
                    args = json.loads(f_args_raw) if isinstance(f_args_raw, str) else f_args_raw
                except Exception:
                    args = {}

                logger.info("Agent 调度工具: %s, 参数: %s", f_name, args)
                tool_output = ""
                if execute_tool:
                    try:
                        tool_output = str(execute_tool(f_name, args))
                    except Exception as e:
                        tool_output = f"工具执行异常: {e}"
                else:
                    tool_output = f"错误：客户端未找到 execute_tool 执行器"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_output[:1200]
                })

        return None, "模型多轮工具调用超出步数上限"
    except Exception as exc:
        return None, f"Agent 交互异常: {exc}"
    finally:
        client.close()


def chat_once(prompt: str, system: Optional[str] = None, timeout: float = 30):
    """单次对话（兜底用）"""
    token = _get_token()
    if not token:
        return None, "未登录，请先在插件中登录账号"
    try:
        server_url = settings.server_url().rstrip("/")
    except Exception as exc:
        return None, f"获取服务器地址失败: {exc}"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {"messages": messages, "active_layers": []}
    client = HttpClient(token=token, request_timeout=timeout, retries=0)
    try:
        resp = client.post(
            f"{server_url}{API_COPILOT_CHAT}", json=payload, auth=True,
            timeout=(10, timeout), retries=0, stream=True,
        )
        if resp.status_code != 200:
            return None, f"服务异常 HTTP {resp.status_code}"
        parts = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break
            try:
                data = json.loads(data_str)
            except Exception:
                continue
            if data.get("type") == "text" and data.get("content"):
                parts.append(data["content"])
        return "".join(parts).strip(), ""
    except Exception as exc:
        return None, f"连接异常: {exc}"
    finally:
        client.close()