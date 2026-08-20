# -*- coding: utf-8 -*-
"""
AI Copilot chat widget — natural language interface for RS/GIS operations.

Features:
- Markdown rich-text rendering
- Collapsible Reasoning (auto-collapses on text output)
- Collapsible Tool Execution Progress
- DeepSeek/R1 thinking mode compliance (strict reasoning_content round-trip & history auto-sanitization)
- Round-scoped buffer separation for multi-step tool call round-trips
- Dynamic pulsing & simulated progress bar for asynchronous background tasks
- On-demand extent guard triggered ONLY when interpretation tasks are executed
- Robust streaming HTML rendering without syntax breakage
"""
import html
import json
import re
import time
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QUrl, QTimer
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QTextBrowser,
    QMessageBox, QFrame, QMenu, QLabel, QApplication,
)
from qgis.PyQt.QtGui import QDesktopServices
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, QgsApplication

from ..api.copilot_task import BackendCopilotTask
from ..core.exceptions import ExtentTooLargeError
from .theme import (
    BTN_CLEAR_QSS, BTN_STOP_QSS, BTN_KEY_QSS, BTN_TOOLS_QSS,
    BTN_SEND_QSS, CHAT_HISTORY_QSS, INPUT_CARD_QSS, MENU_QSS,
)

# 内部技能函数名到人类友好提示文案的映射表
SKILL_HUMAN_LABELS = {
    "get_active_layers": "读取当前图层列表",
    "skill_calc_spectral_index": "计算遥感光谱指数",
    "skill_run_pca": "执行主成分分析 (PCA)",
    "skill_dem_analysis": "分析 DEM 地形要素",
    "skill_spatial_filter": "执行空间滤波与边缘提取",
    "skill_area_statistics": "统计地物分类面积",
    "skill_vector_smooth": "平滑与化简矢量图斑",
    "skill_kmeans_cluster": "执行 K-Means 聚类分析",
    "skill_raster_diff": "双期像元差分变化检测",
    "skill_image_enhance": "影像画质增强与真/假彩色合成",
    "skill_raster_polygonize": "栅格结果矢量化与面要素提取",
    "skill_geocode_address": "地名地址解析与地图定位",
    "skill_ai_extract_feature": "启动云端要素解译大模型",
    "skill_ai_sam3_extract": "启动云端 SAM3 交互提示解译",
    "skill_ai_change_detection": "启动云端深度时相变化检测模型",
    "qgis_search_tools": "检索 QGIS 空间算法工具箱",
    "qgis_get_tool_params": "读取 QGIS 算法参数配置",
    "qgis_run_algorithm": "执行 QGIS 本地空间分析算法",
    "skill_fetch_sentinel2_imagery":"检索并流式加载 Sentinel-2 遥感影像",
    "execute_pyqgis_code":"执行动态 PyQGIS 空间分析代码"
}


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
        self.chat_history: list = []  # 传给后端的标准 OpenAI messages
        self.display_items: list = []  # UI 显示用的结构化卡片列表
        self._llm_task = None
        self._active_ai_task = None
        self._current_assistant_item = None
        self._current_progress_item_id = None

        # 进度条呼吸律动与动态步进定时器
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(500)
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

        self.clear_btn = QPushButton("清空会话")
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

        self.disclaimer_label = QLabel(" ⚠️ 模型可能出错，请提前保存数据 ")
        self.disclaimer_label.setStyleSheet(
            "QLabel { background-color: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; "
            "border-radius: 8px; padding: 4px 8px; font-size: 11px; font-weight: 500; }"
        )
        self.disclaimer_label.setAlignment(Qt.AlignCenter)
        top_bar.addWidget(self.disclaimer_label)
        top_bar.addStretch()
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

        self.btn_key = QPushButton("🔑 账号 / 额度")
        self.btn_key.setToolTip("管理登录凭证及账户额度")
        self.btn_key.setStyleSheet(BTN_KEY_QSS)
        self.btn_key.clicked.connect(self.dock.show_account_page)
        bottom_toolbar.addWidget(self.btn_key)

        self.btn_tools = QPushButton("🧰 Tools 遥感工具箱 ▾")
        self.btn_tools.setToolTip("选择本地 10 大免联网工具或 8 大云端 AI 专项解译")
        self.btn_tools.setStyleSheet(BTN_TOOLS_QSS)
        self._build_tools_menu()
        bottom_toolbar.addWidget(self.btn_tools)

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
            ("🎀 矢量图斑化简与平滑", "task_vector_smooth"),
            ("🍭 K-Means 智能无监督聚类", "task_kmeans"),
            ("🐣 双期像元差分变化检测", "task_raster_diff"),
            ("🌈 假彩色合成与画质增强", "task_enhance"),
            ("🧩 栅格一键矢量化与过滤", "task_polygonize"),
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
                self._render_chat_ui(preserve_scroll=True)
        elif link.startswith("http://") or link.startswith("https://"):
            QDesktopServices.openUrl(url)

    # -- 核心 UI 渲染引擎 ----------------------------------------------------

    def _render_chat_ui(self, preserve_scroll: bool = False):
        sb = self.history_browser.verticalScrollBar()
        old_val = sb.value()
        is_at_bottom = (old_val >= sb.maximum() - 20)

        html_blocks = [
            "<div style='background-color:#f8fafc; border:1px solid #e2e8f0; "
            "padding:12px 16px; margin:4px 0 10px 0; color:#334155; font-size:13px; line-height:1.6;'>"
            "<div style='font-weight:700; color:#0f172a; font-size:14px; margin-bottom:6px;'>"
            "🛰️ GeoMind AI Copilot</div>"
            "<div style='margin-bottom:6px;'>我是您的智能遥感与 GIS 助手，您可以直接在下方输入自然语言指令。</div>"
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
        self._render_chat_ui()

    def _clear_and_new_session(self):
        if self._llm_task is not None or self._active_ai_task is not None:
            reply = QMessageBox.question(
                self, "确认新建会话",
                "当前有正在执行的分析任务，是否强制停止并清空会话？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self._stop_current_task()
        self._progress_timer.stop()
        self.chat_history = []
        self._current_assistant_item = None
        self._current_progress_item_id = None
        self._reset_welcome_message()
        self._reset_btn()

    def _stop_current_task(self):
        stopped = False
        self._progress_timer.stop()
        if self._llm_task is not None:
            try:
                self._llm_task.cancel()
            except Exception:
                pass
            self._llm_task = None
            stopped = True
        if self._active_ai_task is not None:
            try:
                self._active_ai_task.cancel()
            except Exception:
                pass
            self._active_ai_task = None
            stopped = True
        if stopped:
            if self._current_progress_item_id:
                self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
                self._current_progress_item_id = None
            self.display_items.append({"id": f"stop_{time.time()}", "role": "system_error", "content": "⏹ 任务已被用户打断"})
            self._render_chat_ui()
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

        # 日常普通对话不作范围拦截，直接发送
        self.input_edit.clear()

        user_msg_id = f"user_{time.time()}"
        self.display_items.append({"id": user_msg_id, "role": "user", "content": text})
        self.chat_history.append({"role": "user", "content": text})

        self._current_assistant_item = {
            "id": f"asst_{time.time()}",
            "role": "assistant",
            "reasoning": "",         # UI 累加展示
            "round_reasoning": "",   # 仅供当前轮次提交给后端
            "reasoning_collapsed": False,
            "tools": [],
            "tools_collapsed": False,
            "content": "",           # UI 累加展示
            "round_content": "",     # 仅供当前轮次提交给后端
        }
        self.display_items.append(self._current_assistant_item)
        self._render_chat_ui()

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
            self._render_chat_ui()
            self._reset_btn()
            return

        elif msg_type == "reasoning" and content:
            self._current_assistant_item["reasoning"] += content
            self._current_assistant_item["round_reasoning"] += content
            self._render_chat_ui()

        elif msg_type == "text" and content:
            if not self._current_assistant_item["content"] and self._current_assistant_item["reasoning"]:
                self._current_assistant_item["reasoning_collapsed"] = True

            self._current_assistant_item["content"] += content
            self._current_assistant_item["round_content"] += content
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
            self.chat_history.append(asst_msg)

            self._current_assistant_item["round_reasoning"] = ""
            self._current_assistant_item["round_content"] = ""

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                args_raw = tc["function"]["arguments"]
                label = SKILL_HUMAN_LABELS.get(
                    fn_name,
                    f"执行空间算法: {fn_name.replace('skill_', '').replace('_', ' ').title()}"
                )

                tool_record = {"label": label, "status": "running", "result": ""}
                self._current_assistant_item["tools"].append(tool_record)
                self._render_chat_ui()
                QApplication.processEvents()

                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    res = self._execute_local_skill(fn_name, args)
                    tool_record["status"] = "ok"
                    tool_record["result"] = str(res)
                except Exception as e:
                    tool_record["status"] = "error"
                    tool_record["result"] = str(e)
                    res = f"执行报错: {e}"

                self.chat_history.append({
                    "tool_call_id": tc["id"],
                    "role": "tool",
                    "name": fn_name,
                    "content": str(res),
                })
                self._render_chat_ui()

            # 递归请求大模型输出后续分析
            self._request_backend_copilot()

    def _on_copilot_finished(self):
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
                self.chat_history.append(asst_msg)

            self._current_assistant_item["round_content"] = ""
            self._current_assistant_item["round_reasoning"] = ""

        self._reset_btn()

    def _on_copilot_error(self, err_msg: str):
        self.display_items.append(
            {"id": f"err_{time.time()}", "role": "system_error", "content": f"无法获取 AI 回复：{err_msg}"})
        self._render_chat_ui()
        self._reset_btn()

    # -- 技能分发调度 --------------------------------------------------------

    def _execute_local_skill(self, fn_name: str, args: dict) -> str:
        from ..tools.skill_dispatcher import (
            get_active_layers, skill_calc_spectral_index, skill_run_pca,
            skill_dem_analysis, skill_spatial_filter, skill_area_statistics,
            skill_vector_smooth, skill_kmeans_cluster, skill_raster_diff,
            skill_image_enhance, skill_raster_polygonize,
            skill_ai_extract_feature, skill_ai_sam3_extract, skill_ai_change_detection,
            skill_geocode_address, qgis_search_tools, qgis_get_tool_params, qgis_run_algorithm,skill_fetch_sentinel2_imagery, 
            execute_pyqgis_code,
        )
        from ..utils.extent_guard import check_extent_too_large

        server_url = self.dock.current_server_url()
        token = self.dock.token
        machine_id = self.dock.machine_id

        local_tools = {
            "get_active_layers": get_active_layers,
            "skill_calc_spectral_index": skill_calc_spectral_index,
            "skill_run_pca": skill_run_pca,
            "skill_dem_analysis": skill_dem_analysis,
            "skill_spatial_filter": skill_spatial_filter,
            "skill_area_statistics": skill_area_statistics,
            "skill_vector_smooth": skill_vector_smooth,
            "skill_kmeans_cluster": skill_kmeans_cluster,
            "skill_raster_diff": skill_raster_diff,
            "skill_image_enhance": skill_image_enhance,
            "skill_raster_polygonize": skill_raster_polygonize,
            "skill_geocode_address": skill_geocode_address,
            "qgis_search_tools": qgis_search_tools,
            "qgis_get_tool_params": qgis_get_tool_params,
            "qgis_run_algorithm": qgis_run_algorithm,
            "skill_fetch_sentinel2_imagery": skill_fetch_sentinel2_imagery,
            "execute_pyqgis_code": execute_pyqgis_code, 
        }

        if fn_name in local_tools:
            return local_tools[fn_name](**args)

        # 核心：仅在调用云端深度解译大模型时进行范围过大判定与拦截
        if fn_name in ("skill_ai_extract_feature", "skill_ai_sam3_extract", "skill_ai_change_detection"):
            canvas = self.dock.canvas
            extent = canvas.extent()
            extent_crs = canvas.mapSettings().destinationCrs()

            # 提前基于目标图层进行范围超限校验
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
                    self._render_chat_ui()
                    return (
                        f"已停止执行解译：当前地图视口范围过大（{guard_msg}）。"
                        f"请明确告知用户：需要放大地图视图以聚焦解译区域，然后再重新尝试提取。"
                    )

            try:
                if fn_name == "skill_ai_extract_feature":
                    task = skill_ai_extract_feature(
                        args.get("layer_name"), args.get("feature_type"),
                        server_url, token, machine_id, extent=extent, extent_crs=extent_crs,
                    )
                elif fn_name == "skill_ai_sam3_extract":
                    task = skill_ai_sam3_extract(
                        args.get("layer_name"), args.get("prompt"), args.get("output_format", "mask"),
                        server_url, token, machine_id, extent=extent, extent_crs=extent_crs,
                    )
                else:
                    task = skill_ai_change_detection(
                        args.get("layer_t1"), args.get("layer_t2"),
                        server_url, token, machine_id, extent=extent, extent_crs=extent_crs,
                    )
            except ExtentTooLargeError as e:
                self.display_items.append({
                    "id": f"guard_{time.time()}",
                    "role": "system_error",
                    "content": f"解译范围过大已拦截：{e}"
                })
                self._render_chat_ui()
                return f"已停止推送裁图与解译：{e}。请提示用户放大地图后重试。"
            except Exception as e:
                return f"提交云端解译任务失败: {e}"

            self._active_ai_task = task
            self.stop_btn.setEnabled(True)

            # 插入并激活后台进度条卡片
            progress_card_id = f"progress_{time.time()}"
            self._current_progress_item_id = progress_card_id
            self._anim_tick = 0
            self.display_items.append({
                "id": progress_card_id,
                "role": "task_progress",
                "status_text": "影像切片与预处理中",
                "progress": 8.0,
                "icon": "⚡",
            })
            self._render_chat_ui()

            # 启动动画与平滑步进定时器
            self._progress_timer.start()

            # 绑定任务进度与生命周期信号
            try:
                task.progressChanged.connect(self._on_ai_task_progress)
            except Exception:
                pass

            task.taskSucceeded.connect(self._on_ai_task_ok)
            task.taskFailed.connect(self._on_ai_task_error)
            task.taskCancelled.connect(self._on_ai_task_cancelled)

            QgsApplication.taskManager().addTask(task)
            return "已成功向云端 GPU 集群投递解译任务，正在后台处理中..."

        return f"未找到可执行工具: {fn_name}"

    def _on_progress_timer_tick(self):
        """定时器脉冲：驱动图标闪烁与平滑进度渐进"""
        if not self._current_progress_item_id:
            self._progress_timer.stop()
            return

        self._anim_tick += 1
        icons = ["⚡", "✨", "⏳", "⌛"]
        current_icon = icons[self._anim_tick % len(icons)]
        dots = "." * ((self._anim_tick % 3) + 1)

        for item in self.display_items:
            if item.get("id") == self._current_progress_item_id:
                item["icon"] = current_icon
                curr_p = item.get("progress", 0.0)

                # 模拟平滑增长（避免卡在0%）
                if curr_p < 30.0:
                    curr_p += 2.5
                    item["status_text"] = f"影像切片与云端投递中{dots}"
                elif curr_p < 75.0:
                    curr_p += 1.2
                    item["status_text"] = f"云端 GPU 深度模型推理中{dots}"
                elif curr_p < 95.0:
                    curr_p += 0.4
                    item["status_text"] = f"要素图斑矢量化与后处理中{dots}"

                item["progress"] = min(95.0, curr_p)
                self._render_chat_ui(preserve_scroll=True)
                break

    def _on_ai_task_progress(self, progress: float):
        """若后台有真实进度推送，则优先采用最高进度"""
        if self._current_progress_item_id:
            for item in self.display_items:
                if item.get("id") == self._current_progress_item_id:
                    item["progress"] = max(item.get("progress", 0.0), progress)
                    self._render_chat_ui(preserve_scroll=True)
                    break

    def _on_ai_task_cancelled(self):
        """任务取消处理"""
        self._progress_timer.stop()
        if self._current_progress_item_id:
            self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
            self._current_progress_item_id = None
        self.display_items.append({"id": f"ai_cancel_{time.time()}", "role": "system_error", "content": "⏹ 云端解译已取消"})
        self._render_chat_ui()
        self._active_ai_task = None
        self._reset_btn()

    def _on_ai_task_ok(self, result_path, content_type):
        """任务成功处理"""
        self._progress_timer.stop()
        if self._current_progress_item_id:
            self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
            self._current_progress_item_id = None

        layer_name = f"AI解译结果({datetime.now().strftime('%H:%M:%S')})"
        if result_path.endswith(".tif"):
            new_layer = QgsRasterLayer(result_path, layer_name)
        else:
            new_layer = QgsVectorLayer(result_path, layer_name, "ogr")

        if new_layer.isValid():
            QgsProject.instance().addMapLayer(new_layer)
            self.display_items.append({
                "id": f"ai_ok_{time.time()}",
                "role": "assistant",
                "content": f"🎉 **云端解译完成！** 已自动为您加载图层：`{layer_name}`"
            })
        else:
            self.display_items.append({"id": f"ai_fail_{time.time()}", "role": "system_error", "content": "解译结果图层加载失败"})

        self._render_chat_ui()
        self._active_ai_task = None
        self._reset_btn()

    def _on_ai_task_error(self, err_msg):
        """任务失败处理"""
        self._progress_timer.stop()
        if self._current_progress_item_id:
            self.display_items = [item for item in self.display_items if item.get("id") != self._current_progress_item_id]
            self._current_progress_item_id = None

        self.display_items.append(
            {"id": f"ai_err_{time.time()}", "role": "system_error", "content": f"云端解译失败：{err_msg}"})
        self._render_chat_ui()
        self._active_ai_task = None
        self._reset_btn()

    # -- 辅助方法 -----------------------------------------------------------

    def _scroll_to_bottom(self):
        sb = self.history_browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _reset_btn(self):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("🚀 发送")
        self.stop_btn.setEnabled(False)
        self._llm_task = None
        self._scroll_to_bottom()