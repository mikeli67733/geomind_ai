# -*- coding: utf-8 -*-
"""
图标助手 —— 全插件统一的图标加载 / 渲染 / 降级层。

图标来自 Tabler Icons (MIT, https://tabler.io/icons)，随插件打包在
assets/icons/ 目录（含 LICENSE.txt）。图标为 24x24 outline SVG，
由 QtSvg 原样渲染——观感与网页端完全一致。

所有界面图标（按钮 QIcon、菜单图标、聊天内联图、状态标签富文本）
统一经由此模块；图标文件缺失时优雅降级为纯文字，不影响功能。
"""
import os
import re

from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, QRectF, QSize, Qt
from qgis.PyQt.QtGui import QColor, QIcon, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer

_ICONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "icons",
)

DEFAULT_ICON_COLOR = "#334155"
DEFAULT_ICON_SIZE = 16

# 与 theme.py 按钮/文案配色配套的图标颜色常量
ICON_WHITE = "#ffffff"      # 蓝底白字主按钮 / 品牌 logo
ICON_SLATE = "#475569"      # 顶部导航 iconBtn 文字色
ICON_BLUE = "#1d4ed8"       # 工具箱按钮 / 横幅文字色 (BTN_TOOLS / noticeBanner)
ICON_RED = "#dc2626"        # 取消/危险按钮文字色 (cancelBtn / 停止)
ICON_GREEN = "#15803d"      # 进度卡标题绿色 / 成功态
ICON_GRAY = "#64748b"       # 状态标签灰 / 折叠链接灰

_pixmap_cache = {}  # (name, size, color) -> QPixmap
_html_cache = {}    # (name, size, color) -> data-URI <img> HTML


# ---------------------------------------------------------------------------
# 底层渲染
# ---------------------------------------------------------------------------
def _svg_text(name: str) -> str:
    try:
        with open(os.path.join(_ICONS_DIR, name + ".svg"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _render_pixmap(name: str, size: int, color: str):
    """把 Tabler SVG 渲染成 2x 位图（观感与网页一致），带缓存。"""
    key = (name, size, color)
    cached = _pixmap_cache.get(key)
    if cached is not None:
        return cached

    svg = _svg_text(name)
    if not svg:
        return None
    svg = svg.replace("currentColor", color)

    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return None

    scale = 2
    canvas = size * scale
    pm = QPixmap(canvas, canvas)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter, QRectF(pm.rect()))
    painter.end()

    _pixmap_cache[key] = pm
    return pm


# ---------------------------------------------------------------------------
# QIcon 渲染（按钮 / 菜单 / 下拉项）
# ---------------------------------------------------------------------------
def icon(name: str, size: int = DEFAULT_ICON_SIZE, color: str = DEFAULT_ICON_COLOR):
    """按图标名生成 QIcon；文件缺失时返回空 QIcon。"""
    pm = _render_pixmap(name, size, color)
    return QIcon(pm) if pm is not None else QIcon()


def apply_icon(button, name: str, size: int = DEFAULT_ICON_SIZE, color: str = DEFAULT_ICON_COLOR):
    """给 QPushButton / QToolButton 设置图标，保留原有文字。"""
    ic = icon(name, size=size, color=color)
    if not ic.isNull():
        button.setIcon(ic)
        button.setIconSize(QSize(size, size))


# ---------------------------------------------------------------------------
# HTML / QLabel 富文本（内联 PNG data-URI，QTextBrowser 原生支持）
# ---------------------------------------------------------------------------
def icon_html(name: str, size: int = 14, color: str = DEFAULT_ICON_COLOR) -> str:
    """聊天 HTML 内联图标；图标缺失时返回空串（退化为纯文字）。"""
    key = (name, size, color, "html")
    cached = _html_cache.get(key)
    if cached is not None:
        return cached

    pm = _render_pixmap(name, size, color)
    if pm is None:
        return ""

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    pm.toImage().save(buf, "PNG")
    buf.close()

    html = (
        '<img src="data:image/png;base64,{b64}" width="{w}" height="{h}" '
        'style="vertical-align:middle;">'
    ).format(
        b64=bytes(ba.toBase64()).decode("ascii"),
        w=size, h=size,
    )
    _html_cache[key] = html
    return html


def label_html(name: str, text: str = "", color: str = ICON_GRAY, size: int = 14) -> str:
    """QLabel 富文本内容：图标 + 文字。"""
    html = icon_html(name, size=size, color=color)
    if text:
        html += "&nbsp;" + text
    return html


def apply_label(label, name: str, text: str = "", color: str = ICON_GRAY, size: int = 14):
    """设置 QLabel 为富文本（图标+文字）。"""
    label.setTextFormat(Qt.RichText)
    label.setText(label_html(name, text, color=color, size=size))


def chat_document(body_html: str) -> str:
    """包装聊天 HTML 文档（图标已内联为 data-URI，无需外部字体样式）。"""
    return "<html><head></head><body>" + body_html + "</body></html>"


# ---------------------------------------------------------------------------
# LLM 可见文案的 emoji 清洗（技能返回结果）
# ---------------------------------------------------------------------------
_CONTENT_ICON_MAP = {
    # 状态符号 → 通用文本符号（任意字体可渲染，且对 LLM 友好）
    "❌": "✗ ",
    "✔": "✓ ",
    "⚠️": "⚠ ",
    "⚠": "⚠ ",
    "💡": "ℹ ",
    "ℹ️": "ℹ ",
    # 纯装饰 emoji → 直接去除（文字本身已表达含义）
    "🎉": "",
    "⛰️": "",
    "📊": "",
    "📐": "",
    "🧩": "",
    "🔄": "",
    "🎨": "",
    "📦": "",
    "🌍": "",
    "🌳": "",
    "👥": "",
    "🌃": "",
    "💧": "",
    "⛅": "",
    "🛰️": "",
    "📡": "",
    "🗺️": "",
    "📌": "",
    "🔍": "",
    "📄": "",
    "📍": "",
    "✨": "",
    "🏷️": "",
    "🚀": "",
}


def strip_content_icons(text: str) -> str:
    """把技能返回文案里的装饰 emoji 替换为文本符号，并合并多余空格。

    在唯一的展示/上送出口（Copilot 工具结果）统一调用，避免逐文件改
    tools/skills 与 core/prompts 的字符串。
    """
    if not text:
        return text
    for emoji, replacement in _CONTENT_ICON_MAP.items():
        text = text.replace(emoji, replacement)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()
