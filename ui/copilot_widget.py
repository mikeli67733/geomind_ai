# -*- coding: utf-8 -*-
"""
AI Copilot chat widget — natural language interface for RS/GIS operations.

Streams responses from the backend LLM, dispatches tool calls to local
skills or cloud AI tasks, and renders results in a rich text chat view.
"""
import html
import json
from datetime import datetime

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QTextEdit,
    QMessageBox, QFrame, QMenu, QToolButton, QLabel,
)
from qgis.PyQt.QtGui import QTextCursor, QTextCharFormat, QColor
from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer, QgsApplication

from ..api.copilot_task import BackendCopilotTask
from ..core.exceptions import ExtentTooLargeError
from .theme import (
    BTN_CLEAR_QSS, BTN_STOP_QSS, BTN_KEY_QSS, BTN_TOOLS_QSS,
    BTN_SEND_QSS, CHAT_HISTORY_QSS, INPUT_CARD_QSS, MENU_QSS,
)
from ..core.compat import ALIGN_CENTER


class LlmCopilotWidget(QWidget):
    """AI Copilot chat interface with tool-call dispatch."""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self.chat_history: list = []
        self._llm_task = None
        self._active_ai_task = None
        self._current_assistant_reply = ""
        self._assistant_block_open = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # -- Top toolbar --
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

        self.disclaimer_label = QLabel(" ⚠️ 模型可能出错，请在运行前做好数据保存工作 ")
        self.disclaimer_label.setStyleSheet(
            "QLabel { background-color: #fff7ed; color: #c2410c; border: 1px solid #fed7aa; "
            "border-radius: 10px; padding: 4px 10px; font-size: 11px; font-weight: 500; }"
        )
        self.disclaimer_label.setAlignment(Qt.AlignCenter)
        top_bar.addWidget(self.disclaimer_label)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        # -- Chat history --
        self.history_browser = QTextEdit()
        self.history_browser.setReadOnly(True)
        self.history_browser.setStyleSheet(CHAT_HISTORY_QSS)
        self._reset_welcome_message()
        layout.addWidget(self.history_browser, stretch=1)

        # -- Input card --
        input_card = QFrame()
        input_card.setStyleSheet(INPUT_CARD_QSS)
        card_layout = QVBoxLayout(input_card)
        card_layout.setContentsMargins(8, 6, 8, 6)
        card_layout.setSpacing(6)

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("告诉我您的遥感分析或 GIS 需求（按回车直接发送）...")
        self.input_edit.setStyleSheet("border: none; font-size: 13px; padding: 4px; background: transparent;")
        self.input_edit.returnPressed.connect(self._send_msg)
        card_layout.addWidget(self.input_edit)

        # -- Bottom toolbar --
        bottom_toolbar = QHBoxLayout()
        bottom_toolbar.setSpacing(6)

        self.btn_key = QPushButton("🔑 Key / 账号")
        self.btn_key.setToolTip("管理您的 API Key、登录凭证及账户额度")
        self.btn_key.setStyleSheet(BTN_KEY_QSS)
        self.btn_key.clicked.connect(self.dock.show_account_page)
        bottom_toolbar.addWidget(self.btn_key)

        self.btn_tools = QPushButton("🧰 Tools 遥感工具箱 ▾")
        self.btn_tools.setToolTip("打开本地 10 大免费遥感工具与 8 大 AI 专项大模型")
        self.btn_tools.setStyleSheet(BTN_TOOLS_QSS)
        self._build_tools_menu()
        bottom_toolbar.addWidget(self.btn_tools)

        bottom_toolbar.addStretch()

        self.send_btn = QPushButton("发送")
        self.send_btn.setStyleSheet(BTN_SEND_QSS)
        self.send_btn.clicked.connect(self._send_msg)
        bottom_toolbar.addWidget(self.send_btn)

        card_layout.addLayout(bottom_toolbar)
        layout.addWidget(input_card)

    def _build_tools_menu(self):
        """Build the Tools dropdown menu."""
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
            act = tools_menu.addAction(title) if False else free_menu.addAction(title)
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

    # -- Chat management ----------------------------------------------------

    def _close_assistant_block(self):
        """Close the assistant message card if it is still open."""
        if self._assistant_block_open:
            self.history_browser.append("</div>")
            self._assistant_block_open = False

    def _reset_welcome_message(self):
        self.history_browser.setHtml(
            "<div style='background-color:#f8fafc; border:1px solid #e2e8f0; "
            "border-radius:12px; padding:14px 16px; margin:4px 0 14px 0; color:#334155; "
            "font-size:13px; line-height:1.6;'>"
            "<div style='font-weight:700; color:#0f172a; font-size:14px; margin-bottom:8px;'>"
            "GeoMind Copilot</div>"
            "<div style='margin-bottom:10px;'>您好，我是您的智能遥感与 GIS 助手。可以直接输入自然语言指令，"
            "或通过左下角的 <b>Tools 遥感工具箱</b> 选择专项工具面板。</div>"
            "<div style='display:flex; flex-wrap:wrap; gap:8px; margin-top:6px;'>"
            "<span style='background:#eff6ff; color:#1d4ed8; border-radius:6px; padding:3px 8px; "
            "font-size:12px; border:1px solid #bfdbfe;'>图层缓冲区分析</span>"
            "<span style='background:#eff6ff; color:#1d4ed8; border-radius:6px; padding:3px 8px; "
            "font-size:12px; border:1px solid #bfdbfe;'>水体提取与矢量化</span>"
            "<span style='background:#eff6ff; color:#1d4ed8; border-radius:6px; padding:3px 8px; "
            "font-size:12px; border:1px solid #bfdbfe;'>NDVI 植被指数计算</span>"
            "</div></div>"
        )

    def _clear_and_new_session(self):
        if self._llm_task is not None or self._active_ai_task is not None:
            reply = QMessageBox.question(
                self, "确认新建会话",
                "当前有正在进行的任务，是否强制停止并清空会话？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self._stop_current_task()
        self.chat_history = []
        self._reset_welcome_message()
        self._reset_btn()

    def _stop_current_task(self):
        stopped = False
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
            self._close_assistant_block()
            self.history_browser.append(
                "<div style='margin:10px 0;'>"
                "<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; "
                "border-radius:6px; padding:4px 10px; font-size:11px; font-weight:600;'>"
                "任务已打断</span></div>"
            )
            self._reset_btn()

    # -- Send & receive -----------------------------------------------------

    def _send_msg(self):
        text = self.input_edit.text().strip()
        if not text:
            return
        if not self.dock.token:
            QMessageBox.warning(self, "请先登录", "使用 AI Copilot 助手需要先登录您的账号！")
            self.dock.show_account_page()
            return

        # 每次对话前重新检查屏幕范围：过大则提醒并默认停止推送裁图/解译
        too_large, guard_msg = self._check_canvas_extent_too_large()
        if too_large:
            self._append_warning_chip(guard_msg)
            reply = QMessageBox.question(
                self, "屏幕范围过大",
                f"{guard_msg}\n\n是否仍要发送本条消息？\n"
                "普通问答可继续；涉及裁图/解译的任务仍会被拦截。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.input_edit.clear()
        self._close_assistant_block()
        safe_text = html.escape(text)
        self.history_browser.append(
            "<div style='background-color:#eff6ff; border-left:3px solid #3b82f6; "
            "padding:10px 14px; margin:12px 0 4px 24px; color:#1e293b; font-size:13px; "
            "line-height:1.5;'>"
            "<div style='font-weight:700; color:#1d4ed8; font-size:12px; margin-bottom:4px;'>你</div>"
            f"{safe_text}</div>"
        )
        self.send_btn.setEnabled(False)
        self.send_btn.setText("思考中...")
        self.stop_btn.setEnabled(True)
        self.chat_history.append({"role": "user", "content": text})
        self._request_backend_copilot()

    def _check_canvas_extent_too_large(self):
        """
        Re-check the current canvas extent against raster layers.

        Called before every chat message so oversized viewport ranges are
        caught early (bandwidth protection).  Prefers the active raster layer,
        otherwise checks all raster layers.  Returns ``(too_large, message)``
        and passes when no raster layer is available or estimation fails.
        """
        from ..utils.extent_guard import check_extent_too_large

        canvas = getattr(self.dock, "canvas", None)
        iface = getattr(self.dock, "iface", None)
        if canvas is None or iface is None:
            return False, ""
        try:
            extent = canvas.extent()
            extent_crs = canvas.mapSettings().destinationCrs()
        except Exception:
            return False, ""

        active = iface.activeLayer()
        if isinstance(active, QgsRasterLayer):
            candidates = [active]
        else:
            candidates = [
                layer for layer in QgsProject.instance().mapLayers().values()
                if isinstance(layer, QgsRasterLayer)
            ]
        for layer in candidates:
            too_large, msg = check_extent_too_large(layer, extent, extent_crs)
            if too_large:
                return True, msg
        return False, ""

    def _append_warning_chip(self, message: str):
        """Append a red warning chip to the chat history."""
        safe = html.escape(message)
        self.history_browser.append(
            "<div style='margin:10px 0;'>"
            "<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; "
            "border-radius:6px; padding:4px 10px; font-size:11px; font-weight:600;'>"
            f"⚠️ 已停止推送：{safe}</span></div>"
        )

    def _request_backend_copilot(self):
        active_layers = [l.name() for l in QgsProject.instance().mapLayers().values()]
        server_url = self.dock.current_server_url()
        token = self.dock.token
        machine_id = getattr(self.dock, "machine_id", "")

        self._current_assistant_reply = ""
        self._has_started_reasoning = False
        self._has_started_text = False

        task = BackendCopilotTask(server_url, token, self.chat_history, active_layers, machine_id=machine_id)
        task.chunkReceived.connect(self._on_chunk_received)
        task.taskError.connect(self._on_copilot_error)
        task.taskFinished.connect(self._on_copilot_finished)

        self._llm_task = task
        QgsApplication.taskManager().addTask(task)

    def _on_chunk_received(self, data: dict):
        msg_type = data.get("type")
        content = data.get("content", "")

        cursor = self.history_browser.textCursor()
        cursor.movePosition(QTextCursor.End)

        if msg_type == "error":
            self._close_assistant_block()
            safe_content = html.escape(str(content))
            self.history_browser.append(
                "<div style='margin:10px 0;'>"
                "<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; "
                "border-radius:6px; padding:4px 10px; font-size:11px; font-weight:600;'>"
                f"大模型响应异常：{safe_content}</span></div>"
            )
            self._reset_btn()
            return

        elif msg_type == "reasoning" and content:
            if not self._has_started_reasoning:
                self._has_started_reasoning = True
                self.history_browser.append(
                    "<div style='margin:10px 0 6px 0;'>"
                    "<span style='background:#f1f5f9; color:#475569; border-radius:6px; "
                    "padding:3px 8px; font-size:11px; font-weight:600; border:1px solid #e2e8f0;'>"
                    "模型思考过程</span></div>"
                )
                cursor.movePosition(QTextCursor.End)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#64748b"))
            fmt.setFontItalic(True)
            fmt.setFontPointSize(9)
            cursor.insertText(content, fmt)
            self._scroll_to_bottom()

        elif msg_type == "text" and content:
            if not self._has_started_text:
                self._has_started_text = True
                self._close_assistant_block()
                self.history_browser.append(
                    "<div style='background-color:#f8fafc; border-left:3px solid #94a3b8; "
                    "padding:10px 14px; margin:12px 24px 4px 0; color:#1e293b; font-size:13px; "
                    "line-height:1.5;'>"
                    "<div style='font-weight:700; color:#0f172a; font-size:12px; margin-bottom:4px;'>"
                    "Copilot</div>"
                )
                self._assistant_block_open = True
                cursor.movePosition(QTextCursor.End)
            self._current_assistant_reply += content
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#0f172a"))
            fmt.setFontItalic(False)
            fmt.setFontPointSize(10)
            cursor.insertText(content, fmt)
            self._scroll_to_bottom()

        elif msg_type == "tool_call":
            tool_calls = data.get("tool_calls", [])
            self.chat_history.append({
                "role": "assistant",
                "content": self._current_assistant_reply or None,
                "tool_calls": tool_calls,
            })
            self._current_assistant_reply = ""

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                args_raw = tc["function"]["arguments"]
                safe_fn = html.escape(fn_name)
                self.history_browser.append(
                    "<div style='margin:8px 0;'>"
                    "<span style='background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; "
                    "border-radius:6px; padding:4px 10px; font-size:11px; font-weight:600;'>"
                    f"执行本地技能：{safe_fn}</span></div>"
                )
                from qgis.PyQt.QtWidgets import QApplication
                QApplication.processEvents()

                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    res = self._execute_local_skill(fn_name, args)
                except Exception as e:
                    res = f"执行报错: {e}"

                safe_res = html.escape(str(res))
                self.history_browser.append(
                    "<div style='margin:6px 0 10px 0;'>"
                    "<span style='background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0; "
                    "border-radius:6px; padding:4px 10px; font-size:11px; font-weight:500;'>"
                    f"执行结果：{safe_res}</span></div>"
                )
                self.chat_history.append({
                    "tool_call_id": tc["id"],
                    "role": "tool",
                    "name": fn_name,
                    "content": str(res),
                })

            self.history_browser.append(
                "<div style='margin:8px 0;'>"
                "<span style='background:#f8fafc; color:#64748b; border:1px solid #e2e8f0; "
                "border-radius:6px; padding:3px 8px; font-size:11px;'>"
                "本地进程：正在生成最终报告...</span></div>"
            )
            self._request_backend_copilot()

    def _on_copilot_finished(self):
        self._close_assistant_block()
        if self._current_assistant_reply:
            self.chat_history.append({"role": "assistant", "content": self._current_assistant_reply})
            self._current_assistant_reply = ""
        self._reset_btn()

    def _on_copilot_error(self, err_msg: str):
        self._close_assistant_block()
        safe_err = html.escape(err_msg)
        self.history_browser.append(
            "<div style='margin:10px 0;'>"
            "<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; "
            "border-radius:6px; padding:4px 10px; font-size:11px; font-weight:600;'>"
            f"无法获取 AI 回复：{safe_err}</span></div>"
        )
        self._reset_btn()

    # -- Skill dispatch -----------------------------------------------------

    def _execute_local_skill(self, fn_name: str, args: dict) -> str:
        from ..tools.skill_dispatcher import (
            get_active_layers, skill_calc_spectral_index, skill_run_pca,
            skill_dem_analysis, skill_spatial_filter, skill_area_statistics,
            skill_vector_smooth, skill_kmeans_cluster, skill_raster_diff,
            skill_image_enhance, skill_raster_polygonize,
            skill_ai_extract_feature, skill_ai_sam3_extract, skill_ai_change_detection,
            skill_geocode_address, qgis_search_tools, qgis_get_tool_params, qgis_run_algorithm,
        )

        server_url = self.dock.current_server_url()
        token = self.dock.token
        machine_id = self.dock.machine_id

        # Local free tools
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
        }

        if fn_name in local_tools:
            return local_tools[fn_name](**args)

        # Cloud AI tasks
        if fn_name in ("skill_ai_extract_feature", "skill_ai_sam3_extract", "skill_ai_change_detection"):
            canvas = self.dock.canvas
            extent = canvas.extent()
            extent_crs = canvas.mapSettings().destinationCrs()

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
                return f"已停止推送裁图与解译：{e}"
            except Exception as e:
                return f"提交云端解译任务失败: {e}"

            self._active_ai_task = task
            self.stop_btn.setEnabled(True)
            task.progressMessage.connect(
                lambda msg: self.history_browser.append(
                    "<div style='margin:4px 0;'>"
                    "<span style='background:#f8fafc; color:#64748b; border:1px solid #e2e8f0; "
                    "border-radius:6px; padding:3px 8px; font-size:11px;'>"
                    f"云端进度：{html.escape(msg)}</span></div>"
                )
            )
            task.taskSucceeded.connect(self._on_ai_task_ok)
            task.taskFailed.connect(self._on_ai_task_error)
            task.taskCancelled.connect(
                lambda: self.history_browser.append(
                    "<div style='margin:10px 0;'>"
                    "<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; "
                    "border-radius:6px; padding:4px 10px; font-size:11px; font-weight:600;'>"
                    "云端任务已取消</span></div>"
                )
            )
            QgsApplication.taskManager().addTask(task)
            return "已成功向云端 GPU 集群投递解译任务，正在后台处理中..."

        return f"未找到可执行工具: {fn_name}"

    def _on_ai_task_ok(self, result_path, content_type):
        layer_name = f"AI解译结果({datetime.now().strftime('%H:%M:%S')})"
        if result_path.endswith(".tif"):
            new_layer = QgsRasterLayer(result_path, layer_name)
        else:
            new_layer = QgsVectorLayer(result_path, layer_name, "ogr")
        if new_layer.isValid():
            QgsProject.instance().addMapLayer(new_layer)
            self.history_browser.append(
                "<div style='margin:10px 0;'>"
                "<span style='background:#f0fdf4; color:#15803d; border:1px solid #bbf7d0; "
                "border-radius:6px; padding:5px 12px; font-size:12px; font-weight:600;'>"
                f"云端解译完成，已自动加载图层：{layer_name}</span></div>"
            )
        else:
            self.history_browser.append(
                "<div style='margin:10px 0;'>"
                "<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; "
                "border-radius:6px; padding:5px 12px; font-size:12px; font-weight:600;'>"
                "结果图层加载失败</span></div>"
            )
        self._active_ai_task = None
        self._reset_btn()

    def _on_ai_task_error(self, err_msg):
        safe_err = html.escape(err_msg)
        self.history_browser.append(
            "<div style='margin:10px 0;'>"
            "<span style='background:#fef2f2; color:#dc2626; border:1px solid #fecaca; "
            "border-radius:6px; padding:5px 12px; font-size:12px; font-weight:600;'>"
            f"云端解译失败：{safe_err}</span></div>"
        )
        self._active_ai_task = None
        self._reset_btn()

    # -- UI helpers ---------------------------------------------------------

    def _scroll_to_bottom(self):
        sb = self.history_browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _reset_btn(self):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("🚀 发送")
        self.stop_btn.setEnabled(False)
        self._llm_task = None
        self._scroll_to_bottom()
