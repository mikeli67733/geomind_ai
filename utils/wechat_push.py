# -*- coding: utf-8 -*-
"""
企业微信群机器人推送 —— 7x24 监控任务的默认通信通道。

群机器人在企业微信群里添加后获得 webhook URL，本模块向其发送
markdown 消息（官方群机器人消息格式），失败自动重试并返回错误信息。
"""
import time
from typing import Optional

from ..core.logger import get_logger

logger = get_logger("utils.wechat_push")

_HTTP = None


def _session():
    """惰性创建共享 requests 会话（连接池复用）。"""
    global _HTTP
    if _HTTP is None:
        import requests
        _HTTP = requests.Session()
    return _HTTP


def push_markdown(webhook_url: str, content: str, retries: int = 2) -> Optional[str]:
    """推送 markdown 消息到企业微信群机器人。

    成功返回 None；失败返回人类可读的错误信息。
    """
    if not webhook_url or not webhook_url.strip().startswith("http"):
        return "未配置企业微信 Webhook URL"
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    last_err = ""
    for attempt in range(retries + 1):
        try:
            resp = _session().post(webhook_url.strip(), json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                if data.get("errcode") == 0:
                    return None
                last_err = f"企业微信返回错误: {data.get('errmsg', data)}"
            else:
                last_err = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = f"网络异常: {exc}"
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    logger.warning("企业微信推送失败: %s", last_err)
    return last_err


def push_text(webhook_url: str, content: str, retries: int = 2) -> Optional[str]:
    """推送纯文本消息。"""
    if not webhook_url or not webhook_url.strip().startswith("http"):
        return "未配置企业微信 Webhook URL"
    payload = {"msgtype": "text", "text": {"content": content}}
    last_err = ""
    for attempt in range(retries + 1):
        try:
            resp = _session().post(webhook_url.strip(), json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                if data.get("errcode") == 0:
                    return None
                last_err = f"企业微信返回错误: {data.get('errmsg', data)}"
            else:
                last_err = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = f"网络异常: {exc}"
        if attempt < retries:
            time.sleep(1.0 * (attempt + 1))
    return last_err
