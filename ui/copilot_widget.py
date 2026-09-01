# -*- coding: utf-8 -*-
"""
AI Copilot chat widget — natural language interface for RS/GIS operations.

Performance & Stability Upgrades:
- 60ms UI Render Throttler (reduces setHtml calls by 90%, ultra-smooth)
- Event pump integration to prevent UI freezing during network I/O
- Markdown rich-text rendering with lightweight regex caching
- Collapsible Reasoning (auto-collapses on text output)
- Anti-Loop Deadlock Guard for asynchronous tool calls
- Concurrent Multi-Task Queue with Semantic Layer Naming
"""
import html
import json
import re
import time
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QUrl, QTimer, QCoreApplication
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QTextBrowser,
    QMessageBox, QFrame, QMenu, QLabel, QApplication,
)
from qgis.PyQt.QtGui import QDesktopServices
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, QgsApplication

from ..api.copilot_task import BackendCopilotTask
from ..core.exceptions import ExtentTooLargeError
from ..core.history import history_store
from ..core.logger import get_logger
from ..utils.extent_tool import ExtentSelectTool
from .theme import (
    BTN_CLEAR_QSS, BTN_STOP_QSS, BTN_KEY_QSS, BTN_TOOLS_QSS,
    BTN_SEND_QSS, CHAT_HISTORY_QSS, INPUT_CARD_QSS, MENU_QSS,
)

logger = get_logger(__name__)


# 内部技能函数名的展示文案统一由 tools.skill_registry 提供
from ..tools.skill_registry import skill_label as _skill_registry_label


def _skill_human_label(fn_name: str) -> str:
    return _skill_registry_label(fn_name)


def _render_mini_markdown(raw_text: str) -> str:
    """将 Markdown 转换为符合 Qt Rich Text 规范的 HTML。"""
    if not raw_text:
        return ""

    text = html.escape(raw_text)

    # 1. 独立代码块 ```code```
    def replace_code_block(m):
        code = m.group(1).strip()
        return (
            "<div style='background-color:#1e293b; color:#f8fafc; border-radius:6px; "
            "padding:8px 10px; margin:6px 0; font-family:Consolas, Monaco, monospace; "
            f"font-size:11px; white-space:pre-wrap;'>{code}</div>"
        )

    text = re.sub(r"```(?:\w+)?\n?(.*?)```", replace_code_block, text, flags=re.DOTALL)

    # 2. 行内代码 `code`
    text = re.sub(
        r"`([^`]+)`",
        r"<span style='background-color:#f1f5f9; color:#0f172a; padding:1px 5px; border-radius:4px; font-family:Consolas, monospace; font-size:11px; border:1px solid #e2e8f0;'>\1</span>",
        text
    )

    # 3. 标题 (##, ###)
    text = re.sub(r"^(?:###\s+)(.+)$",
                  r"<div style='font-size:13px; font-weight:700; color:#0f172a; margin:8px 0 4px 0;'>\1</div>", text,
                  flags=re.MULTILINE)
    text = re.sub(r"^(?:##\s+)(.+)$",
                  r"<div style='font-size:14px; font-weight:700; color:#0f172a; margin:10px 0 4px 0;'>\1</div>", text,
                  flags=re.MULTILINE)

    # 4. 粗体与斜体
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", text)

    # 5. 无序列表
    text = re.sub(r"^(?:[\*\-]\s+)(.+)$", r"<div style='margin-left:8px;'>• \1</div>", text, flags=re.MULTILINE)

    # 6. 换行
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return text


class LlmCopilotWidget(QWidget):
    """具有折叠思考、实时动态进度条与流式卡片渲染的 AI Copilot 助手。"""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self.chat_history: list = []
        self.display_items: list = []
        self._llm_task = None
        self._active_ai_tasks: dict = {}
        self._current_assistant_item = None
        self._current_progress_item_id = None
        self._consecutive_tool_calls: list = []
        self._chat_session_dir = None   # history/copilot/<timestamp>/ folder

        # 核心性能优化：UI 渲染节流时间戳
        self._last_render_ts = 0.0

        # 进度条呼吸律动与动态步进定时器
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(600)
        self._progress_timer.timeout.connect(self._on_progress_timer_tick)
        self._anim_tick = 0

        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # -- 顶部工具栏 --
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(2, 0, 2, 0)
        top_bar.setSpacing(6)

        self.clear_btn = QPushButton("新建会话")
        self.clear_btn.setToolTip("清空历史记录并开启新会话")
        self.clear_btn.setStyleSheet(BTN_CLEAR_QSS)
        self.clear_btn.clicked.connect(self._clear_and_new_session)
        top_bar.addWidget(self.clear_btn)

        self.stop_btn = QPushButton("停止生成")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setToolTip("中断当前 AI 思考或任务")
        self.stop_btn.setStyleSheet(BTN_STOP_QSS)
        self.stop_btn.clicked.connect(self._stop_current_task)
        top_bar.addWidget(self.stop_btn)

        top_bar.addStretch()

        self.disclaimer_label = QLabel(" ⚠️ 模型可能出错，请提前保存数据 ")
        self.disclaimer_label.setStyleSheet(
            "QLabel { background-color: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; "
            "border-radius: 8px; padding: 4px 8px; font-size: 11px; font-weight: 500; }"
        )
        self.disclaimer_label.setAlignment(Qt.AlignCenter)
        top_bar.addWidget(self.disclaimer_label)
        layout.addLayout(top_bar)

        # -- 会话浏览区 --
        self.history_browser = QTextBrowser()
        self.history_browser.setReadOnly(True)
        self.history_browser.setOpenLinks(False)
        self.history_browser.anchorClicked.connect(self._on_anchor_clicked)
        self.history_browser.setStyleSheet(CHAT_HISTORY_QSS)
        layout.addWidget(self.history_browser, stretch=1)

        # -- 输入区域卡片 --
        input_card = QFrame()
        input_card.setStyleSheet(INPUT_CARD_QSS)
        card_layout = QVBoxLayout(input_card)
        card_layout.setContentsMargins(8, 6, 8, 6)
        card_layout.setSpacing(6)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("输入遥感解译、GIS 空间分析需求（按 Enter 发送）...")
        self.input_edit.setStyleSheet("border: none; font-size: 13px; padding: 4px; background: transparent;")
        self.input_edit.returnPressed.connect(self._send_msg)
        card_layout.addWidget(self.input_edit)

        # -- 底部工具栏 --
        bottom_toolbar = QHBoxLayout()
        bottom_toolbar.setSpacing(6)

        # self.btn_key = QPushButton("🔑 账号 / 额度")
        # self.btn_key.setToolTip("管理登录凭证及账户额度")
        # self.btn_key.setStyleSheet(BTN_KEY_QSS)
        # self.btn_key.clicked.connect(self.dock.show_account_page)
        # bottom_toolbar.addWidget(self.btn_key)
        self.btn_select = QPushButton("🗺 框选范围")
        self.btn_select.setToolTip("点击后在地图上拖拽框选全局解译范围（全局生效，AI 解译页自动使用；未框选时自动使用当前视图范围）")
        self.btn_select.setStyleSheet(BTN_TOOLS_QSS)
        self.btn_select.clicked.connect(self._start_extent_select)
        bottom_toolbar.addWidget(self.btn_select)

        self.btn_tools = QPushButton("🧰 Tools 遥感工具箱 ▾")
        self.btn_tools.setToolTip("选择本地 10 大免联网工具或 8 大云端 AI 专项解译")
        self.btn_tools.setStyleSheet(BTN_TOOLS_QSS)
        self._build_tools_menu()
        bottom_toolbar.addWidget(self.btn_tools)

        # self.selection_chip = QLabel("🗺 未选图层 · 未框选范围")
        # self.selection_chip.setObjectName("noticeBanner")
        # self.selection_chip.setToolTip("主页全局图层/范围选择状态，可点击「图层 / 范围」修改")
        # bottom_toolbar.addWidget(self.selection_chip)

        bottom_toolbar.addStretch()

        self.send_btn = QPushButton("🚀 发送")
        self.send_btn.setStyleSheet(BTN_SEND_QSS)
        self.send_btn.clicked.connect(self._send_msg)
        bottom_toolbar.addWidget(self.send_btn)

        card_layout.addLayout(bottom_toolbar)
        layout.addWidget(input_card)

        self._reset_welcome_message()

    def _build_tools_menu(self):
        tools_menu = QMenu(self)
        tools_menu.setStyleSheet(MENU_QSS)

        free_menu = tools_menu.addMenu("🎈 本地免费遥感工具箱 (0额度/免联网)")
        free_menu.setStyleSheet(MENU_QSS)
        free_tools = [
            ("🍀 全能遥感光谱指数库", "task_spectral_index"),
            ("🔮 遥感 PCA 主成分分析", "task_pca"),
            ("🗻 DEM 地形全要素分析", "task_dem"),
            ("🔎 空间滤波与边缘提取", "task_filter"),
            ("🍰 地物分类面积统计报表", "task_area"),
            ("🍭 K-Means 智能无监督聚类", "task_kmeans"),
            ("🐣 双期像元差分变化检测", "task_raster_diff"),
            ("🌈 假彩色合成与画质增强", "task_enhance"),
        ]
        for title, key in free_tools:
            act = free_menu.addAction(title)
            act.triggered.connect(lambda chk=False, k=key: self.dock.navigate_to_task(k))

        ai_menu = tools_menu.addMenu("🧠 AI 深度学习专项解译大模型")
        ai_menu.setStyleSheet(MENU_QSS)
        ai_tools = [
            ("🌻 土地利用全要素综合解译", "task_landuse_multi"),
            ("🏡 建筑物专项提取", "task_building"),
            ("🚗 道路交通专项提取", "task_road"),
            ("🐬 水系水体专项提取", "task_water"),
            ("🍄 林草植被专项提取", "task_vegetation"),
            ("🥕 农田耕地专项提取", "task_farmland"),
            ("🌟 SAM3 交互提示解译", "task_sam3"),
            ("🐥 深度双期影像变化检测", "task_change"),
        ]
        for title, key in ai_tools:
            act = ai_menu.addAction(title)
            act.triggered.connect(lambda chk=False, k=key: self.dock.navigate_to_task(k))

        self.btn_tools.setMenu(tools_menu)

    # -- 主页全局「图层 / 范围」选择 -------------------------------------------

    def _selection_chip_text(self) -> str:
        extent = getattr(self.dock, "global_extent", None)
        if extent is not None:
            return (
                f"🗺 框选范围 X[{extent.xMinimum():.0f}–{extent.xMaximum():.0f}] "
                f"Y[{extent.yMinimum():.0f}–{extent.yMaximum():.0f}]")
        return "🗺 未框选范围（将使用当前视图）"

    def update_selection_chip(self):
        """Refresh the global-selection status chip, if it exists in the UI."""
        if hasattr(self, "selection_chip"):
            self.selection_chip.setText(self._selection_chip_text())
        else:
            self._show_selection_hint(self._selection_chip_text())

    def _show_selection_hint(self, text: str):
        """Transient status hint; falls back to the brand subtitle label."""
        if hasattr(self, "selection_chip"):
            self.selection_chip.setText(text)
        else:
            self.dock.subtitle_label.setText(text)

    def _start_extent_select(self):
        self._extent_select_tool = ExtentSelectTool(self.dock.canvas)
        self._extent_select_tool.extentSelected.connect(self._on_global_extent_selected)
        self.dock.canvas.setMapTool(self._extent_select_tool)
        self._show_selection_hint("🐾 请在地图上按住左键拖拽框选范围…")

    def _on_global_extent_selected(self, rect):
        self.dock.set_global_selection(extent=rect)

    # -- 交互式链接与折叠事件分发 --------------------------------------------

    def _on_anchor_clicked(self, url: QUrl):
        link = url.toString()
        if link.startswith("toggle:"):
            parts = link.split(":")
            if len(parts) >= 3:
                target_type = parts[1]
                msg_id = parts[2]
                for item in self.display_items:
                    if item.get("id") == msg_id:
                        if target_type == "reasoning":
                            item["reasoning_collapsed"] = not item.get("reasoning_collapsed", False)
                        elif target_type == "tools":
                            item["tools_collapsed"] = not item.get("tools_collapsed", False)
                        break
                self._render_chat_ui(preserve_scroll=True, force=True)
        elif link.startswith("http://") or link.startswith("https://"):
            QDesktopServices.openUrl(url)

    # -- 核心 UI 渲染引擎 (带节流抗卡顿) --------------------------------------

    def _render_chat_ui(self, preserve_scroll: bool = False, force: bool = False):
        """
        高性能节流渲染器：
        在 Token 高频输出时，最多每 60ms 刷新一次 DOM，避免 CPU 100% 满载卡死。
        """
        now = time.time()
        if not force and (now - self._last_render_ts < 0.06):
            return
        self._last_render_ts = now

        sb = self.history_browser.verticalScrollBar()
        old_val = sb.value()
        is_at_bottom = (old_val >= sb.maximum() - 20)

        html_blocks = [
            "<div style='background-color:#f8fafc; border:1px solid #e2e8f0; "
            "padding:12px 16px; margin:4px 0 10px 0; color:#334155; font-size:13px; line-height:1.6;'>"
            # "<div style='font-weight:700; color:#0f172a; font-size:14px; margin-bottom:6px;'>"
            # "🤖 GeoMind AI Copilot</div>"
            "<div style='margin-bottom:6px;'>🤖 我是您的智能遥感与 GIS 助手，您可以直接在下方输入自然语言指令。</div>"
            "<div><span style='background:#eff6ff; color:#1d4ed8; padding:2px 6px; font-size:11px; border:1px solid #bfdbfe;'>计算影像 NDVI</span> "
            "<span style='background:#eff6ff; color:#1d4ed8; padding:2px 6px; font-size:11px; border:1px solid #bfdbfe;'>水体提取与矢量化</span> "
            "<span style='background:#eff6ff; color:#1d4ed8; padding:2px 6px; font-size:11px; border:1px solid #bfdbfe;'>地斑缓冲分析</span></div></div>"
        ]

        for item in self.display_items:
            role = item["role"]
            item_id = item["id"]

            if role == "user":
                safe_text = html.escape(item["content"]).replace("\n", "<br>")
                html_blocks.append(
                    "<div style='background-color:#eff6ff; border:1px solid #dbeafe; border-left:3px solid #3b82f6; "
                    "padding:9px 12px; margin:8px 0 4px 18px; color:#1e293b; font-size:13px; line-height:1.5;'>"
                    "<div style='font-weight:700; color:#1d4ed8; font-size:12px; margin-bottom:3px;'>👤</div>"
                    f"{safe_text}</div>"
                )

            elif role == "task_progress":
                progress = item.get("progress", 0.0)
                status_text = item.get("status_text", "正在处理中...")
                icon = item.get("icon", "⚡")
                pct = max(0, min(100, int(progress)))
                html_blocks.append(
                    "<div style='background-color:#f0fdf4; border:1px solid #bbf7d0; border-left:3px solid #16a34a; "
                    "padding:10px 12px; margin:8px 0; font-size:12px; line-height:1.5;'>"
                    f"<div style='font-weight:700; color:#15803d; margin-bottom:6px;'>{icon} {html.escape(status_text)} ({pct}%)</div>"
                    "<div style='background-color:#e2e8f0; border-radius:4px; height:8px; width:100%; overflow:hidden;'>"
                    f"<div style='background-color:#16a34a; width:{max(4, pct)}%; height:100%; border-radius:4px;'></div>"
                    "</div></div>"
                )

            elif role == "system_error":
                safe_text = html.escape(item["content"])
                html_blocks.append(
                    "<div style='margin:6px 0;'>"
                    f"<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; padding:3px 8px; font-size:11px; font-weight:600;'>⚠️ {safe_text}</span></div>"
                )

            elif role == "assistant":
                sub_blocks = []

                # (A) 思考过程卡片
                reasoning = item.get("reasoning", "")
                if reasoning:
                    is_collapsed = item.get("reasoning_collapsed", False)
                    char_count = len(reasoning)
                    if is_collapsed:
                        sub_blocks.append(
                            "<div style='margin:4px 0;'>"
                            f"<a href='toggle:reasoning:{item_id}' style='text-decoration:none; color:#64748b; font-size:11px; font-weight:600; "
                            "background-color:#f1f5f9; border:1px solid #e2e8f0; border-radius:4px; padding:3px 8px; display:inline-block;'>"
                            f"▶ 🤔 模型思考过程 ({char_count} 字 · 点击展开)</a></div>"
                        )
                    else:
                        formatted_r = _render_mini_markdown(reasoning)
                        sub_blocks.append(
                            "<div style='background-color:#f8fafc; border:1px solid #e2e8f0; border-left:3px solid #94a3b8; "
                            "padding:8px 10px; margin:6px 0;'>"
                            "<div style='margin-bottom:4px;'>"
                            f"<a href='toggle:reasoning:{item_id}' style='text-decoration:none; color:#475569; font-size:11px; font-weight:700;'>"
                            "▼ 🤔 模型思考过程 (点击折叠)</a></div>"
                            f"<div style='color:#64748b; font-size:11px; line-height:1.5;'>{formatted_r}</div></div>"
                        )

                # (B) 工具调用与处理进度
                tools = item.get("tools", [])
                if tools:
                    is_tools_collapsed = item.get("tools_collapsed", False)
                    tool_count = len(tools)
                    if is_tools_collapsed:
                        sub_blocks.append(
                            "<div style='margin:4px 0;'>"
                            f"<a href='toggle:tools:{item_id}' style='text-decoration:none; color:#0369a1; font-size:11px; font-weight:600; "
                            "background-color:#f0f9ff; border:1px solid #bae6fd; border-radius:4px; padding:3px 8px; display:inline-block;'>"
                            f"▶ ⚙️ 处理进度 (已执行 {tool_count} 个步骤 · 点击展开)</a></div>"
                        )
                    else:
                        tool_lines = []
                        for t in tools:
                            label = t.get("label", "执行工具")
                            status = t.get("status", "running")
                            res = t.get("result", "")
                            if status == "running":
                                tool_lines.append(
                                    f"<div style='margin:2px 0; color:#1d4ed8;'>⚙️ {html.escape(label)}...</div>")
                            elif status == "ok":
                                safe_res = html.escape(str(res))
                                tool_lines.append(
                                    f"<div style='margin:2px 0; color:#15803d;'>✔ {html.escape(label)}: <span style='color:#334155;'>{safe_res}</span></div>")
                            elif status == "error":
                                tool_lines.append(
                                    f"<div style='margin:2px 0; color:#dc2626;'>❌ {html.escape(label)} 失败: {html.escape(str(res))}</div>")

                        sub_blocks.append(
                            "<div style='background-color:#f8fafc; border:1px solid #e0f2fe; border-left:3px solid #0284c7; "
                            "padding:6px 10px; margin:6px 0;'>"
                            "<div style='margin-bottom:4px;'>"
                            f"<a href='toggle:tools:{item_id}' style='text-decoration:none; color:#0369a1; font-size:11px; font-weight:700;'>"
                            "▼ ⚙️ 工具与空间处理进度 (点击折叠)</a></div>"
                            f"<div style='font-size:11px; line-height:1.5;'>{''.join(tool_lines)}</div></div>"
                        )

                # (C) 回复正文卡片
                content = item.get("content", "")
                if content:
                    formatted_c = _render_mini_markdown(content)
                    sub_blocks.append(
                        "<div style='background-color:#f8fafc; border:1px solid #e2e8f0; border-left:3px solid #2563eb; "
                        "padding:10px 14px; margin:8px 18px 4px 0; color:#1e293b; font-size:13px; line-height:1.55;'>"
                        "<div style='font-weight:700; color:#0f172a; font-size:12px; margin-bottom:4px;'>GeoMind Copilot</div>"
                        f"<div>{formatted_c}</div></div>"
                    )

                if sub_blocks:
                    html_blocks.append("".join(sub_blocks))

        self.history_browser.setHtml("".join(html_blocks))

        if preserve_scroll and not is_at_bottom:
            sb.setValue(old_val)
        else:
            self._scroll_to_bottom()

    def _reset_welcome_message(self):
        self.display_items = []
        self._current_progress_item_id = None
        self._progress_timer.stop()
        self._consecutive_tool_calls = []
        self._active_ai_tasks.clear()
        self._render_chat_ui(force=True)

    # -- 会话持久化（历史记录 / 回溯） ---------------------------------------

    def _append_chat(self, msg: dict, extra: dict = None):
        """Append to in-memory history and persist to the Copilot session folder.

        ``extra`` carries structured metadata (e.g. ``result_path`` of an AI
        interpretation run) so the history page can show the actual outputs.
        """
        self.chat_history.append(msg)
        try:
            if self._chat_session_dir is None:
                self._chat_session_dir = history_store.new_session_dir("copilot")
            payload = {
                "role": msg.get("role", ""),
                "content": msg.get("content") or "",
            }
            if extra:
                payload.update(extra)
            history_store.append_message(self._chat_session_dir, payload)
        except Exception as exc:
            logger.debug("Failed to persist chat message: %s", exc)

    def load_chat_session(self, session_dir: str):
        """Restore a saved Copilot session into the chat view for re-running.

        Rebuilds the visible conversation from the session jsonl and keeps the
        session folder open so follow-up messages append to the same record.
        """
        if not session_dir:
            return
        messages = history_store.read_session_chat(session_dir)
        if not messages:
            return
        self.finalize_chat_session()
        self._progress_timer.stop()
        self.chat_history = []
        self.display_items = []
        self._active_ai_tasks.clear()
        self._consecutive_tool_calls = []
        self._current_assistant_item = None
        self._current_progress_item_id = None
        for m in messages:
            role = m.get("role", "")
            content = m.get("content") or ""
            if role == "user":
                self.display_items.append(
                    {"id": f"hist_user_{time.time()}", "role": "user", "content": content})
                self.chat_history.append({"role": "user", "content": content})
            elif role == "assistant":
                self.display_items.append({
                    "id": f"hist_asst_{time.time()}", "role": "assistant",
                    "reasoning": "", "round_reasoning": "",
                    "reasoning_collapsed": True, "tools": [], "tools_collapsed": True,
                    "content": content, "round_content": "",
                })
                self.chat_history.append({"role": "assistant", "content": content})
        self._chat_session_dir = session_dir
        self._reset_btn()
        self._render_chat_ui(force=True)

    def finalize_chat_session(self):
        """Close the current chat session so the history page sees it complete."""
        if self._chat_session_dir:
            try:
                history_store.finalize_session(self._chat_session_dir)
            except Exception as exc:
                logger.debug("Failed to finalize chat session: %s", exc)
            self._chat_session_dir = None

    def _clear_and_new_session(self):
        if self._llm_task is not None or len(self._active_ai_tasks) > 0:
            reply = QMessageBox.question(
                self, "确认新建会话",
                "当前有正在执行的分析任务，是否强制停止并清空会话？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self._stop_current_task()
        self._progress_timer.stop()
        self.finalize_chat_session()
        self.chat_history = []
        self._consecutive_tool_calls = []
        self._active_ai_tasks.clear()
        self._current_assistant_item = None
        self._current_progress_item_id = None
        self._reset_welcome_message()
        self._reset_btn()

    def _stop_current_task(self):
        stopped = False
        self._progress_timer.stop()
        self._consecutive_tool_calls = []
        if self._llm_task is not None:
            try:
                self._llm_task.cancel()
            except Exception:
                pass
            self._llm_task = None
            stopped = True

        for task_id, task_info in list(self._active_ai_tasks.items()):
            task = task_info.get("task")
            if task is not None:
                try:
                    task.cancel()
                except Exception:
                    pass
            stopped = True
        self._active_ai_tasks.clear()

        if stopped:
            if self._current_progress_item_id:
                self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
                self._current_progress_item_id = None
            self.display_items.append({"id": f"stop_{time.time()}", "role": "system_error", "content": "⏹ 任务已被用户手动打断"})
            self._render_chat_ui(force=True)
            self._reset_btn()

    # -- 核心：DeepSeek 历史上下文强力清洗器 --------------------------------

    def _sanitize_chat_history(self):
        for msg in self.chat_history:
            if msg.get("role") == "assistant":
                if "tool_calls" in msg:
                    if not msg.get("reasoning_content"):
                        msg["reasoning_content"] = "Thinking and preparing tool execution..."
                else:
                    if "reasoning_content" in msg and msg["reasoning_content"] is None:
                        msg["reasoning_content"] = ""

    # -- 消息收发与大模型交互 ------------------------------------------------

    def _send_msg(self):
        text = self.input_edit.text().strip()
        if not text:
            return
        if not self.dock.token:
            QMessageBox.warning(self, "请先登录", "使用 AI Copilot 助手需要先登录您的账号！")
            self.dock.show_account_page()
            return

        self.input_edit.clear()
        self._consecutive_tool_calls = []

        user_msg_id = f"user_{time.time()}"
        self.display_items.append({"id": user_msg_id, "role": "user", "content": text})
        self._append_chat({"role": "user", "content": text})

        self._current_assistant_item = {
            "id": f"asst_{time.time()}",
            "role": "assistant",
            "reasoning": "",
            "round_reasoning": "",
            "reasoning_collapsed": False,
            "tools": [],
            "tools_collapsed": False,
            "content": "",
            "round_content": "",
        }
        self.display_items.append(self._current_assistant_item)
        self._render_chat_ui(force=True)

        self.send_btn.setEnabled(False)
        self.send_btn.setText("思考中...")
        self.stop_btn.setEnabled(True)
        self._request_backend_copilot()

    def _request_backend_copilot(self):
        self._sanitize_chat_history()

        active_layers = [l.name() for l in QgsProject.instance().mapLayers().values()]
        server_url = self.dock.current_server_url()
        token = self.dock.token
        machine_id = getattr(self.dock, "machine_id", "")

        task = BackendCopilotTask(server_url, token, self.chat_history, active_layers, machine_id=machine_id)
        task.chunkReceived.connect(self._on_chunk_received)
        task.taskError.connect(self._on_copilot_error)
        task.taskFinished.connect(self._on_copilot_finished)

        self._llm_task = task
        QgsApplication.taskManager().addTask(task)

    def _on_chunk_received(self, data: dict):
        msg_type = data.get("type")
        content = data.get("content", "")

        if not self._current_assistant_item:
            return

        if msg_type == "error":
            self.display_items.append(
                {"id": f"err_{time.time()}", "role": "system_error", "content": f"大模型响应异常：{content}"})
            self._render_chat_ui(force=True)
            self._reset_btn()
            return

        elif msg_type == "reasoning" and content:
            self._current_assistant_item["reasoning"] += content
            self._current_assistant_item["round_reasoning"] += content
            # 使用节流渲染，极大减轻卡顿
            self._render_chat_ui()

        elif msg_type == "text" and content:
            if not self._current_assistant_item["content"] and self._current_assistant_item["reasoning"]:
                self._current_assistant_item["reasoning_collapsed"] = True

            self._current_assistant_item["content"] += content
            self._current_assistant_item["round_content"] += content
            # 使用节流渲染，极大减轻卡顿
            self._render_chat_ui()

        elif msg_type == "tool_call":
            tool_calls = data.get("tool_calls", [])

            round_reasoning = self._current_assistant_item.get("round_reasoning", "")
            round_content = self._current_assistant_item.get("round_content", "")

            reasoning_val = round_reasoning if round_reasoning else "Thinking and analyzing tool selection..."

            asst_msg = {
                "role": "assistant",
                "content": round_content or None,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning_val,
            }
            self._append_chat(asst_msg)

            self._current_assistant_item["round_reasoning"] = ""
            self._current_assistant_item["round_content"] = ""

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                args_raw = tc["function"]["arguments"]
                self._consecutive_tool_calls.append(fn_name)

                label = _skill_human_label(fn_name)

                tool_record = {"label": label, "status": "running", "result": ""}
                self._current_assistant_item["tools"].append(tool_record)
                self._render_chat_ui(force=True)
                QCoreApplication.processEvents()

                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    res = self._execute_local_skill(fn_name, args)
                    tool_record["status"] = "ok"
                    tool_record["result"] = str(res)
                except Exception as e:
                    tool_record["status"] = "error"
                    tool_record["result"] = str(e)
                    res = f"执行报错: {e}"

                self._append_chat({
                    "tool_call_id": tc["id"],
                    "role": "tool",
                    "name": fn_name,
                    "content": str(res),
                })
                self._render_chat_ui(force=True)

            self._request_backend_copilot()

    def _on_copilot_finished(self):
        # 1. 标记 LLM 任务已完全结束
        self._llm_task = None

        if self._current_assistant_item:
            round_content = self._current_assistant_item.get("round_content", "")
            round_reasoning = self._current_assistant_item.get("round_reasoning", "")
            if round_content or round_reasoning:
                asst_msg = {
                    "role": "assistant",
                    "content": round_content
                }
                if round_reasoning:
                    asst_msg["reasoning_content"] = round_reasoning
                self._append_chat(asst_msg)

            self._current_assistant_item["round_content"] = ""
            self._current_assistant_item["round_reasoning"] = ""

        self._render_chat_ui(force=True)
        self._reset_btn()

    def _on_copilot_error(self, err_msg: str):
        # 1. 标记 LLM 任务已结束
        self._llm_task = None

        self.display_items.append(
            {"id": f"err_{time.time()}", "role": "system_error", "content": f"无法获取 AI 回复：{err_msg}"})
        self._render_chat_ui(force=True)
        self._reset_btn()

    # -- 技能分发调度与并发任务管理 (白名单注册表版) -------------------------

    def _execute_local_skill(self, fn_name: str, args: dict) -> str:
        from ..tools import skill_dispatcher
        from ..tools.skill_registry import get_skill
        from ..utils.extent_guard import check_extent_too_large
        from ..core.prompts import GUARD_LAYER_POLLING, AI_TASK_SUBMITTED, EXTENT_TOO_LARGE_REPLY

        session = self.dock.current_session()
        server_url = session.server_url
        token = session.token
        machine_id = session.machine_id

        # 刷新事件队列，防止网络请求前界面被判定为卡死
        QCoreApplication.processEvents()

        # 核心防刷死锁拦截
        if fn_name == "get_active_layers":
            layer_calls = [name for name in self._consecutive_tool_calls if name == "get_active_layers"]
            if len(layer_calls) >= 2:
                return GUARD_LAYER_POLLING

        # 1. 云端深度解译并发任务提交
        if fn_name in ("skill_ai_extract_feature", "skill_ai_sam3_extract", "skill_ai_change_detection"):
            canvas = self.dock.canvas
            _, global_extent = self.dock.global_selection()
            if global_extent is not None:
                # 优先使用主页框选/选定的范围，而不是当前地图视口。
                extent = global_extent
                extent_crs = canvas.mapSettings().destinationCrs()
            else:
                extent = canvas.extent()
                extent_crs = canvas.mapSettings().destinationCrs()

            target_layer_name = args.get("layer_name") or args.get("layer_t1")
            target_layer = None
            if target_layer_name:
                for l in QgsProject.instance().mapLayers().values():
                    if l.name() == target_layer_name and isinstance(l, QgsRasterLayer):
                        target_layer = l
                        break

            if target_layer is not None:
                too_large, guard_msg = check_extent_too_large(target_layer, extent, extent_crs)
                if too_large:
                    self.display_items.append({
                        "id": f"guard_{time.time()}",
                        "role": "system_error",
                        "content": f"解译范围过大已拦截：{guard_msg}"
                    })
                    self._render_chat_ui(force=True)
                    return EXTENT_TOO_LARGE_REPLY.format(reason=guard_msg)

            task_semantic_name = "AI解译"
            try:
                if fn_name == "skill_ai_extract_feature":
                    feat = args.get("feature_type", "地物")
                    task_semantic_name = f"AI_{feat}"
                    task = skill_dispatcher.skill_ai_extract_feature(
                        args.get("layer_name"), args.get("feature_type"),
                        server_url, token, machine_id, extent=extent, extent_crs=extent_crs,
                    )
                elif fn_name == "skill_ai_sam3_extract":
                    prompt_tag = args.get("prompt", "SAM3").replace(" ", "_")
                    task_semantic_name = f"SAM3_{prompt_tag}"
                    task = skill_dispatcher.skill_ai_sam3_extract(
                        args.get("layer_name"), args.get("prompt"), args.get("output_format", "mask"),
                        server_url, token, machine_id, extent=extent, extent_crs=extent_crs,
                    )
                else:
                    task_semantic_name = "AI_时相变化"
                    task = skill_dispatcher.skill_ai_change_detection(
                        args.get("layer_t1"), args.get("layer_t2"),
                        server_url, token, machine_id, extent=extent, extent_crs=extent_crs,
                    )
            except ExtentTooLargeError as e:
                self.display_items.append({
                    "id": f"guard_{time.time()}",
                    "role": "system_error",
                    "content": f"解译范围过大已拦截：{e}"
                })
                self._render_chat_ui(force=True)
                return f"已停止推送裁图与解译：{e}。请提示用户放大地图后重试。"
            except Exception as e:
                return f"提交云端解译任务失败: {e}"

            task_id = f"task_{time.time()}_{len(self._active_ai_tasks)}"
            self._active_ai_tasks[task_id] = {
                "task": task,
                "semantic_name": task_semantic_name,
                "progress": 8.0,
            }
            self.stop_btn.setEnabled(True)

            if not self._current_progress_item_id:
                progress_card_id = f"progress_{time.time()}"
                self._current_progress_item_id = progress_card_id
                self._anim_tick = 0
                self.display_items.append({
                    "id": progress_card_id,
                    "role": "task_progress",
                    "status_text": f"正在处理 {len(self._active_ai_tasks)} 个云端解译任务",
                    "progress": 8.0,
                    "icon": "⚡",
                })
                self._render_chat_ui(force=True)

            if not self._progress_timer.isActive():
                self._progress_timer.start()

            try:
                task.progressChanged.connect(lambda p, tid=task_id: self._on_ai_task_progress(p, tid))
            except Exception:
                pass

            task.taskSucceeded.connect(lambda r_path, c_type, tid=task_id: self._on_ai_task_ok(r_path, c_type, tid))
            task.taskFailed.connect(lambda err_msg, tid=task_id: self._on_ai_task_error(err_msg, tid))
            task.taskCancelled.connect(lambda tid=task_id: self._on_ai_task_cancelled(tid))

            QgsApplication.taskManager().addTask(task)
            return AI_TASK_SUBMITTED.format(task_name=task_semantic_name)

        # 2. 本地算子经白名单注册表执行（拒绝未注册的任意函数名）
        spec = get_skill(fn_name)
        if spec is None:
            return f"未找到可执行工具或工具未在白名单注册: {fn_name}"
        try:
            res = str(spec.func(**args))
            QCoreApplication.processEvents()
            return res
        except Exception as e:
            return f"执行算子 `{fn_name}` 失败: {e}"

    def _on_progress_timer_tick(self):
        """定时器脉冲：驱动图标闪烁与平滑进度渐进"""
        if not self._current_progress_item_id or not self._active_ai_tasks:
            self._progress_timer.stop()
            return

        self._anim_tick += 1
        icons = ["⚡", "✨", "⏳", "⌛"]
        current_icon = icons[self._anim_tick % len(icons)]
        dots = "." * ((self._anim_tick % 3) + 1)
        active_count = len(self._active_ai_tasks)

        for item in self.display_items:
            if item.get("id") == self._current_progress_item_id:
                item["icon"] = current_icon
                curr_p = item.get("progress", 0.0)

                if curr_p < 35.0:
                    curr_p += 2.5
                    item["status_text"] = f"正在进行 {active_count} 个任务的影像切片与云端投递{dots}"
                elif curr_p < 80.0:
                    curr_p += 1.2
                    item["status_text"] = f"云端 GPU 深度大模型并行推理中 ({active_count}个任务){dots}"
                elif curr_p < 95.0:
                    curr_p += 0.4
                    item["status_text"] = f"要素图斑矢量化与后处理中{dots}"

                item["progress"] = min(95.0, curr_p)
                self._render_chat_ui(preserve_scroll=True, force=True)
                break

    def _on_ai_task_progress(self, progress: float, task_id: str):
        if task_id in self._active_ai_tasks:
            self._active_ai_tasks[task_id]["progress"] = max(self._active_ai_tasks[task_id]["progress"], progress)
        if self._current_progress_item_id and self._active_ai_tasks:
            avg_p = sum(t["progress"] for t in self._active_ai_tasks.values()) / len(self._active_ai_tasks)
            for item in self.display_items:
                if item.get("id") == self._current_progress_item_id:
                    item["progress"] = max(item.get("progress", 0.0), avg_p)
                    self._render_chat_ui(preserve_scroll=True)
                    break

    def _on_ai_task_cancelled(self, task_id: str):
        task_info = self._active_ai_tasks.pop(task_id, {})
        semantic_name = task_info.get("semantic_name", "AI任务")

        if not self._active_ai_tasks:
            self._progress_timer.stop()
            if self._current_progress_item_id:
                self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
                self._current_progress_item_id = None
            self._reset_btn()

        self.display_items.append({"id": f"ai_cancel_{time.time()}", "role": "system_error", "content": f"⏹ 【{semantic_name}】已取消"})
        self._render_chat_ui(force=True)

    def _on_ai_task_ok(self, result_path: str, content_type: str, task_id: str):
        task_info = self._active_ai_tasks.pop(task_id, {})
        semantic_name = task_info.get("semantic_name", "AI解译")
        time_str = datetime.now().strftime("%H%M%S")

        layer_name = f"{semantic_name}_{time_str}"

        if result_path.endswith(".tif"):
            new_layer = QgsRasterLayer(result_path, layer_name)
        else:
            new_layer = QgsVectorLayer(result_path, layer_name, "ogr")

        if new_layer.isValid():
            QgsProject.instance().addMapLayer(new_layer)
            self.display_items.append({
                "id": f"ai_ok_{time.time()}",
                "role": "assistant",
                "content": f"🎉 **【{semantic_name}】完成！** 已自动为您加载图层：`{layer_name}`"
            })
            self._append_chat({
                "role": "system",
                "content": f"【系统通知】后台任务完成：地物类型为【{semantic_name}】的图层已成功加载至工程，图层准确名称为: '{layer_name}'。"
            }, extra={"result_path": result_path, "result_layer": layer_name})
        else:
            self.display_items.append({
                "id": f"ai_fail_{time.time()}",
                "role": "system_error",
                "content": f"【{semantic_name}】结果图层加载失败"
            })

        # =========================================================================
        # 当本批次所有异步 AI 任务全部执行完成时
        # =========================================================================
        if not self._active_ai_tasks:
            self._progress_timer.stop()
            if self._current_progress_item_id:
                self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
                self._current_progress_item_id = None

            # 1. 注入一条系统自动推进指令（明确告知模型图层已就绪，继续执行后续步骤）
            self._append_chat({
                "role": "user",
                "content": "【系统自动推进】本批次所有后台解译图层均已加载完毕。请检查用户最初的需求中是否还有未完成的后续步骤（如缓冲区分析、空间相交叠置、面积统计等），请立即调用相应工具继续执行；如果已全部完成，请直接输出分析总结汇报。"
            })

            # 2. 准备新的 Assistant 消息卡片
            self._current_assistant_item = {
                "id": f"asst_{time.time()}",
                "role": "assistant",
                "reasoning": "",
                "round_reasoning": "",
                "reasoning_collapsed": False,
                "tools": [],
                "tools_collapsed": False,
                "content": "",
                "round_content": "",
            }
            self.display_items.append(self._current_assistant_item)

            # 3. 更新按钮状态为“思考中”并允许用户中断
            self.send_btn.setEnabled(False)
            self.send_btn.setText("思考中...")
            self.stop_btn.setEnabled(True)

            # 4. 立即唤醒大模型发起下一轮自动执行
            self._request_backend_copilot()
        else:
            # 还有其他并发任务在跑，仅重置渲染
            self._render_chat_ui(force=True)

        self._render_chat_ui(force=True)

    def _on_ai_task_error(self, err_msg: str, task_id: str):
        task_info = self._active_ai_tasks.pop(task_id, {})
        semantic_name = task_info.get("semantic_name", "AI任务")

        if not self._active_ai_tasks:
            self._progress_timer.stop()
            if self._current_progress_item_id:
                self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
                self._current_progress_item_id = None
            self._reset_btn()

        self.display_items.append(
            {"id": f"ai_err_{time.time()}", "role": "system_error", "content": f"【{semantic_name}】云端解译失败：{err_msg}"})
        self._render_chat_ui(force=True)

    # -- 辅助方法 -----------------------------------------------------------

    def _scroll_to_bottom(self):
        """双保险置底：先移动 QTextCursor，再延迟一帧等待 Qt HTML 布局计算完毕强行拉到底部。"""
        from qgis.PyQt.QtGui import QTextCursor
        self.history_browser.moveCursor(QTextCursor.End)
        QTimer.singleShot(20, self._force_scroll_bottom)

    def _force_scroll_bottom(self):
        sb = self.history_browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _reset_btn(self):
        if not self._active_ai_tasks and self._llm_task is None:
            self.send_btn.setEnabled(True)
            self.send_btn.setText("🚀 发送")
            self.stop_btn.setEnabled(False)
            self._consecutive_tool_calls = []
            self._scroll_to_bottom()