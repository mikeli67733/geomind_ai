# -*- coding: utf-8 -*-
"""Map canvas screenshot skill — visual reference for multimodal LLMs."""
from ...core.logger import get_logger
from ...utils.map_capture import queue_map_screenshot

logger = get_logger("tools.skills.map_screenshot")


def skill_capture_map_view() -> str:
    """截取当前 QGIS 地图画布作为视觉参考，截图随下一轮消息附给模型。"""
    shot = queue_map_screenshot(reason="模型请求查看地图", force=True)
    if shot is None:
        return "❌ 地图截图失败：画布不可用或渲染超时。"
    return (
        "✔ 已截取当前地图画布。"
        f"{shot['caption']}"
        "截图将随下一轮消息附上，请结合画面分析。"
    )
