# -*- coding: utf-8 -*-
"""
QGIS 地图画布截图工具——为多模态大模型提供视觉参考。

截图在插件主线程调用（工具执行、消息发送、任务回调均为主线程）。
截图以 base64 JPEG data-URI 暂存于进程内队列，由 Copilot 页在下一轮
请求时注入为一条带图 user 消息；自动截图带节流（默认 10 秒内最多 1
张），模型主动调用 ``capture_map_view`` 时 force=True 绕过节流。
"""
import base64
import time
from typing import Dict, Optional

from qgis.PyQt.QtCore import QBuffer, QIODevice
from qgis.utils import iface

#: 自动截图最小间隔（秒）
_AUTO_CAPTURE_MIN_INTERVAL = 10.0
#: 输出 JPEG 最长边像素
_MAX_SIDE = 1280
#: JPEG 质量
_JPEG_QUALITY = 65

_pending: Optional[Dict] = None        # 待注入的截图 {data_uri, caption}
_last_capture_ts: float = 0.0


def _capture_canvas_data_uri(canvas) -> Optional[str]:
    """抓取画布 → 等比缩放 → JPEG base64 data-URI；失败返回 None。"""
    try:
        if hasattr(canvas, "waitWhileRendering"):
            canvas.waitWhileRendering()
        pixmap = canvas.grab()
        if pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
            return None
        longest = max(pixmap.width(), pixmap.height())
        if longest > _MAX_SIDE:
            pixmap = pixmap.scaled(
                pixmap.width() * _MAX_SIDE // longest,
                pixmap.height() * _MAX_SIDE // longest,
            )
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if not pixmap.save(buf, "JPG", _JPEG_QUALITY) or buf.size() <= 0:
            return None
        b64 = base64.b64encode(bytes(buf.data())).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


def _caption(canvas, reason: str) -> str:
    """生成截图说明文字，帮助小模型解读画面。"""
    names = []
    try:
        from qgis.core import QgsProject
        names = [l.name() for l in QgsProject.instance().mapLayers().values()]
    except Exception:
        pass
    layer_text = "、".join(names[:5]) if names else "（无图层）"
    try:
        ext = canvas.extent()
        ext_text = (f"画布范围 X[{ext.xMinimum():.0f}–{ext.xMaximum():.0f}] "
                    f"Y[{ext.yMinimum():.0f}–{ext.yMaximum():.0f}]")
    except Exception:
        ext_text = "画布范围未知"
    return f"原因: {reason}; 图层: {layer_text}; {ext_text}"


def queue_map_screenshot(reason: str = "", force: bool = False) -> Optional[dict]:
    """截取当前画布并加入待发送队列。

    force=True 用于模型主动调用 capture_map_view，绕过时间节流；
    自动截图在节流窗口内直接返回 None（不重复截图）。返回排队信息
    （供技能构造返回文案），失败返回 None。
    """
    global _pending, _last_capture_ts
    if not force and (time.time() - _last_capture_ts) < _AUTO_CAPTURE_MIN_INTERVAL:
        return None
    canvas = iface.mapCanvas() if iface else None
    if canvas is None:
        return None
    data_uri = _capture_canvas_data_uri(canvas)
    if data_uri is None:
        return None
    _last_capture_ts = time.time()
    _pending = {"data_uri": data_uri, "caption": _caption(canvas, reason)}
    return _pending


def take_pending_screenshot() -> Optional[dict]:
    """取出待发送的截图（一次性），供 Copilot 页注入下一轮消息。"""
    global _pending
    shot, _pending = _pending, None
    return shot
