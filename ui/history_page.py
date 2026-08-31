# -*- coding: utf-8 -*-
"""
History page — browse, search and backtrack every run of every page.

Layout:
    [search 🔍] [page filter ▾] [refresh] [clear]      🧠 memory summary
    [🚀 quick launch: top-3 most used pages]
    ┌──────────── record list ─────────┬──── detail ────────────┐
    │ ▌ 今天                            │ status/time/duration   │
    │  ✅ page | summary   14:32        │ params / log / outputs │
    │ ▌ 昨天                            │ [load][rerun][folder]  │
    │  ❌ page | summary   昨天 18:01   │ [delete]               │
    └───────────────────────────────────┴────────────────────────┘
        [加载更多]                        (smart suggestion card)

Logic details:
- live search with a 300 ms debounce (no Enter needed);
- records are grouped under date section headers, newest first;
- list is paginated (_PAGE_SIZE per batch) with a 「加载更多」 button so
  years of history don't stall the UI;
- double-click a record (or 「↩ 重新执行」) jumps to its page with the
  saved parameters pre-filled;
- the smart-suggestion card shows what the memory store would pre-fill
  for the currently filtered page.
"""
import html
import json
import os
from datetime import datetime, timedelta

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QListWidget, QListWidgetItem, QTextBrowser, QMessageBox,
    QSplitter, QFrame,
)

from ..core.history import history_store
from ..core.memory import memory_store

_STATUS_ICONS = {"ok": "✅", "failed": "❌", "cancelled": "⏹️", "session": "💬"}
_STATUS_COLORS = {"ok": "#15803d", "failed": "#dc2626", "cancelled": "#94a3b8"}
_PAGE_SIZE = 60


def _fmt_duration(ms: int) -> str:
    if not ms:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} 分 {int(seconds % 60)} 秒"


def _fmt_time_relative(iso: str) -> str:
    """Today → HH:MM, yesterday → 昨天 HH:MM, else MM-DD HH:MM."""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or ""
    now = datetime.now()
    if dt.date() == now.date():
        return f"今天 {dt.strftime('%H:%M')}"
    if dt.date() == (now - timedelta(days=1)).date():
        return f"昨天 {dt.strftime('%H:%M')}"
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def _date_section_label(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "更早"
    now = datetime.now()
    if dt.date() == now.date():
        return "今天"
    if dt.date() == (now - timedelta(days=1)).date():
        return "昨天"
    if dt.date() >= (now - timedelta(days=7)).date():
        return "最近 7 天"
    if dt.year == now.year:
        return f"{dt.month} 月"
    return f"{dt.year} 年"


class HistoryPage(QWidget):
    """History & memory page."""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._records = []        # merged run records + copilot sessions
        self._shown_count = 0     # pagination cursor
        self._pending_filter = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # -- Toolbar row 1: search + filter + actions -------------------
        toolbar = QHBoxLayout()
        toolbar.setSpacing(2)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索摘要 / 参数 / 页面…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(200)
        self.search_edit.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self.search_edit)

        self.page_filter = QComboBox()
        self.page_filter.setFixedWidth(180)
        self.page_filter.currentIndexChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self.page_filter)

        self.refresh_btn = QPushButton("🔄 刷新")
        self.refresh_btn.clicked.connect(lambda: self.refresh(keep_selection=False))
        toolbar.addWidget(self.refresh_btn)

        self.clear_btn = QPushButton("🗑 清空")
        self.clear_btn.setObjectName("cancelBtn")
        self.clear_btn.clicked.connect(self._on_clear)
        toolbar.addWidget(self.clear_btn)

        layout.addLayout(toolbar)

        # -- Toolbar row 2: quick launch of most-used pages -------------
        toolbar = QHBoxLayout()
        toolbar.setSpacing(2)

        toolbar.addStretch()

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("noticeBanner")
        self.summary_label.setWordWrap(True)
        toolbar.addWidget(self.summary_label, stretch=1)

        layout.addLayout(toolbar)

        # -- Toolbar row 3: quick launch of most-used pages -------------
        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        self.quick_label = QLabel("🚀 常用:")
        self.quick_label.setStyleSheet("color:#64748b; font-size:12px;")
        quick_row.addWidget(self.quick_label)
        self._quick_buttons = []
        for _ in range(3):
            btn = QPushButton()
            btn.setStyleSheet(
                "QPushButton { background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe;"
                "border-radius:8px; padding:4px 10px; font-size:12px; }"
                "QPushButton:hover { background:#dbeafe; }")
            btn.clicked.connect(self._on_quick_launch)
            btn.setVisible(False)
            quick_row.addWidget(btn)
            self._quick_buttons.append(btn)
        quick_row.addStretch()
        self.suggestion_label = QLabel("")
        self.suggestion_label.setStyleSheet("color:#64748b; font-size:11px;")
        self.suggestion_label.setWordWrap(True)
        quick_row.addWidget(self.suggestion_label, stretch=1)
        layout.addLayout(quick_row)

        # -- Split: list + detail ---------------------------------------
        splitter = QSplitter(Qt.Horizontal)

        list_panel = QWidget()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(4)

        self.record_list = QListWidget()
        self.record_list.setObjectName("historyList")
        self.record_list.setUniformItemSizes(False)
        self.record_list.currentItemChanged.connect(self._on_select)
        self.record_list.itemDoubleClicked.connect(lambda _item: self._on_rerun())
        list_layout.addWidget(self.record_list, stretch=1)

        self.load_more_btn = QPushButton("加载更多…")
        self.load_more_btn.clicked.connect(self._load_more)
        self.load_more_btn.setVisible(False)
        list_layout.addWidget(self.load_more_btn)

        splitter.addWidget(list_panel)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(6)

        self.detail_browser = QTextBrowser()
        self.detail_browser.setOpenExternalLinks(False)
        detail_layout.addWidget(self.detail_browser, stretch=1)

        # ========== 第一行按钮 ==========
        action_row1 = QHBoxLayout()
        action_row1.setSpacing(6)
        self.load_result_btn = QPushButton("🖱 加载结果图层")
        self.load_result_btn.clicked.connect(self._on_load_result)
        action_row1.addWidget(self.load_result_btn)

        self.rerun_btn = QPushButton("↩ 重新执行")
        self.rerun_btn.setObjectName("runBtn")
        self.rerun_btn.clicked.connect(self._on_rerun)
        action_row1.addWidget(self.rerun_btn)

        self.open_folder_btn = QPushButton("📁 打开文件夹")
        self.open_folder_btn.clicked.connect(self._on_open_folder)
        action_row1.addWidget(self.open_folder_btn)

        action_row1.addStretch()
        detail_layout.addLayout(action_row1)

        # ========== 第二行按钮 ==========
        action_row2 = QHBoxLayout()
        action_row2.setSpacing(6)

        self.delete_btn = QPushButton("🗑 删除")
        self.delete_btn.setObjectName("cancelBtn")
        self.delete_btn.clicked.connect(self._on_delete)
        action_row2.addWidget(self.delete_btn)

        action_row2.addStretch()
        detail_layout.addLayout(action_row2)

        splitter.addWidget(detail_panel)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, stretch=1)

        # Debounced live search
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(lambda: self.refresh(keep_selection=False))

        self._set_action_buttons_enabled(False)

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------
    def _page_choices(self):
        choices = [("", "全部页面"), ("copilot", "💬 AI 对话")]
        for key, (title, _idx) in sorted(self.dock.task_pages.items()):
            choices.append((key, title))
        return choices

    def refresh(self, keep_selection: bool = True):
        """Reload record list, filter combo, quick launch and summary."""
        keyword = self.search_edit.text().strip()
        page_key = self.page_filter.currentData() or ""
        if self._pending_filter is not None:
            page_key = self._pending_filter

        # Rebuild filter choices only when they change (dock pages are
        # registered after this page is constructed), preserving selection.
        current_choices = [(self.page_filter.itemData(i), self.page_filter.itemText(i))
                           for i in range(self.page_filter.count())]
        new_choices = self._page_choices()
        if current_choices != new_choices:
            self.page_filter.blockSignals(True)
            self.page_filter.clear()
            for key, title in new_choices:
                self.page_filter.addItem(title, key)
            if page_key:
                idx = self.page_filter.findData(page_key)
                if idx >= 0:
                    self.page_filter.setCurrentIndex(idx)
            self.page_filter.blockSignals(False)
        elif page_key:
            idx = self.page_filter.findData(page_key)
            if idx >= 0 and self.page_filter.currentIndex() != idx:
                self.page_filter.blockSignals(True)
                self.page_filter.setCurrentIndex(idx)
                self.page_filter.blockSignals(False)
        self._pending_filter = None

        self._records = []
        if page_key != "copilot":
            self._records += history_store.list_records(
                page_key=page_key or None, keyword=keyword)
        if not page_key or page_key == "copilot":
            if not keyword:
                for session in history_store.list_copilot_sessions():
                    session = dict(session)
                    session["_type"] = "copilot"
                    self._records.append(session)
        self._records.sort(
            key=lambda r: (r.get("created_at") or r.get("last_used_at") or ""),
            reverse=True,
        )

        selected_folder = None
        if keep_selection:
            item = self.record_list.currentItem()
            if item:
                selected_folder = item.data(Qt.UserRole)

        self.record_list.blockSignals(True)
        self.record_list.clear()
        self._shown_count = 0
        self._append_records_page(selected_folder)
        self.record_list.blockSignals(False)

        self._update_summary()
        self._update_quick_launch()
        self._update_suggestion(page_key)

        if self.record_list.count() == 0:
            self.detail_browser.setHtml(
                "<p style='color:#94a3b8;'>暂无历史记录 — 运行任意工具或开始一次 AI 对话后，"
                "这里会按页面归档每次运行的参数与结果。</p>")
            self._set_action_buttons_enabled(False)
        elif not self.record_list.currentItem():
            self.record_list.setCurrentRow(0)
        else:
            self._on_select(self.record_list.currentItem(), None)

    def _append_records_page(self, selected_folder=None):
        """Append the next batch of records to the list with date headers."""
        batch = self._records[self._shown_count:self._shown_count + _PAGE_SIZE]
        last_section = None
        for record in batch:
            time_iso = record.get("created_at") or record.get("last_used_at") or ""
            section = _date_section_label(time_iso)
            if section != last_section:
                self._add_section_item(section)
                last_section = section
            item = self._make_list_item(record)
            self.record_list.addItem(item)
            if selected_folder and record.get("folder") == selected_folder:
                self.record_list.setCurrentItem(item)
        self._shown_count += len(batch)
        remaining = len(self._records) - self._shown_count
        self.load_more_btn.setVisible(remaining > 0)
        if remaining > 0:
            self.load_more_btn.setText(f"加载更多…（还有 {remaining} 条）")

    def _add_section_item(self, label: str):
        item = QListWidgetItem(label)
        item.setFlags(Qt.NoItemFlags)  # not selectable
        item.setForeground(Qt.gray)
        f = item.font()
        f.setBold(True)
        item.setFont(f)
        self.record_list.addItem(item)

    def _make_list_item(self, record: dict) -> QListWidgetItem:
        if record.get("_type") == "copilot":
            icon = "💬"
            title = "AI 对话"
            count = int(record.get("message_count", 0) or 0)
            summary = f"{count} 条消息"
        else:
            status = record.get("status", "")
            icon = _STATUS_ICONS.get(status, "•")
            title = record.get("page_title") or memory_store.page_title(
                record.get("page_key", ""))
            summary = record.get("summary") or record.get("page_key", "")
        time_str = _fmt_time_relative(record.get("created_at") or record.get("last_used_at") or "")
        item = QListWidgetItem(f"{icon}{summary}\n{time_str}")
        item.setData(Qt.UserRole, record.get("folder"))
        item.setData(Qt.UserRole + 1, record.get("_type", "run"))

        status = record.get("status", "session")
        color = _STATUS_COLORS.get(status)
        if color:
            item.setForeground(QColor(color))
        return item

    def _update_summary(self):
        stats = memory_store.stats()
        most = memory_store.most_used_pages(3)
        most_text = "、".join(
            f"{memory_store.page_title(k)}({c}次)" for k, c in most
        ) or "暂无"
        self.summary_label.setText(
            f"🧠 累计 {stats['total_runs']} 次运行 · 最常用：{most_text} · 当前 {len(self._records)} 条记录"
        )

    def _update_quick_launch(self):
        """Fill the quick-launch row with the 3 most used pages."""
        most = memory_store.most_used_pages(3)
        for idx, btn in enumerate(self._quick_buttons):
            if idx < len(most):
                page_key, count = most[idx]
                if page_key == "copilot":
                    btn.setText("💬 AI 对话")
                    btn.setProperty("page_key", "copilot")
                else:
                    title, _ = self.dock.task_pages.get(page_key, (page_key, 0))
                    btn.setText(f"{title} · {count}次")
                    btn.setProperty("page_key", page_key)
                btn.setVisible(True)
                btn.setToolTip("点击直接打开该页面")
            else:
                btn.setVisible(False)
        self.quick_label.setVisible(len(most) > 0)

    def _on_quick_launch(self):
        btn = self.sender()
        page_key = btn.property("page_key")
        if not page_key:
            return
        if page_key == "copilot":
            self.dock.show_copilot_page()
        else:
            self.dock.navigate_to_task(page_key)

    def _update_suggestion(self, page_key: str):
        """Show what the memory would pre-fill for the filtered page."""
        if not page_key or page_key == "copilot":
            self.suggestion_label.setText("")
            return
        suggested = memory_store.suggested_params(page_key)
        if not suggested:
            self.suggestion_label.setText(
                f"💡 尚无「{memory_store.page_title(page_key)}」的使用习惯记录")
            return
        parts = [
            f"{k}={v}" if len(str(v)) <= 24 else f"{k}={str(v)[:24]}…"
            for k, v in list(suggested.items())[:6]
        ]
        self.suggestion_label.setText(
            f"💡 智能记忆将预填：{', '.join(parts)}"
        )

    # ------------------------------------------------------------------
    # Selection / detail
    # ------------------------------------------------------------------
    def _selected(self):
        item = self.record_list.currentItem()
        if not item:
            return None
        folder = item.data(Qt.UserRole)
        for record in self._records:
            if record.get("folder") == folder:
                return record
        return None

    def _on_select(self, current, _previous):
        if not current:
            self._set_action_buttons_enabled(False)
            return
        record = self._selected()
        if not record:
            return
        if record.get("_type") == "copilot":
            self._show_copilot_detail(record)
        else:
            self._show_run_detail(record)

    def _show_run_detail(self, record: dict):
        folder = record.get("folder", "")
        log_text = history_store.read_log(folder)
        params = history_store.load_params(folder)
        outputs = record.get("outputs", [])
        out_lines = []
        for out in outputs:
            path = out.get("path", "")
            if out.get("copied"):
                out_lines.append(f"📦 已存副本: <code style='font-size:11px;'>{html.escape(path)}</code>")
            else:
                out_lines.append(f"📄 仅记录路径: <code style='font-size:11px;'>{html.escape(path)}</code>")
        inputs = record.get("input_layers", [])
        input_lines = []
        for layer in inputs:
            if isinstance(layer, dict) and layer.get("_type") == "layer":
                input_lines.append(
                    f"{html.escape(str(layer.get('name', '')))} "
                    f"<span style='color:#94a3b8;'>[{html.escape(str(layer.get('id', '')))}]</span>")
            else:
                input_lines.append(html.escape(str(layer)))

        status = record.get("status", "")
        color = _STATUS_COLORS.get(status, "#334155")
        html_parts = [
            f"<h2 style='margin-bottom:2px;'>{html.escape(record.get('page_title') or record.get('page_key') or '')}</h2>",
            f"<p style='color:{color}; font-weight:600;'>"
            f"{_STATUS_ICONS.get(status, '')} {_status_label(status)}"
            f"<span style='color:#94a3b8; font-weight:400;'> · "
            f"{_fmt_time_relative(record.get('created_at', ''))} · "
            f"耗时 {_fmt_duration(record.get('duration_ms', 0))}</span></p>",
        ]
        if record.get("summary"):
            html_parts.append(f"<p>{html.escape(record['summary'])}</p>")
        if record.get("error"):
            html_parts.append(
                f"<div style='background:#fef2f2;border:1px solid #fecaca;border-radius:8px;"
                f"padding:8px 10px;color:#dc2626;font-size:12px;'>⚠️ "
                f"{html.escape(str(record['error']))}</div>")
        if input_lines:
            html_parts.append(_section("输入图层", "<br>".join(input_lines)))
        if out_lines:
            html_parts.append(_section("结果文件", "<br>".join(out_lines)))
        if params:
            pretty = json.dumps(params, ensure_ascii=False, indent=2)
            html_parts.append(_section("参数快照",
                f"<pre style='margin:4px 0 0 0; font-size:11px; color:#334155;'>"
                f"{html.escape(pretty)}</pre>"))
        if log_text:
            html_parts.append(_section("运行日志",
                f"<pre style='margin:4px 0 0 0; font-size:11px; color:#334155;'>"
                f"{html.escape(log_text)}</pre>"))
        self.detail_browser.setHtml("".join(html_parts))
        self._set_action_buttons_enabled(True)
        self.load_result_btn.setEnabled(bool(out_lines))
        self.rerun_btn.setEnabled(record.get("page_key") in self.dock.task_pages)

    def _show_copilot_detail(self, record: dict):
        folder = record.get("folder", "")
        messages = history_store.read_session_chat(folder)
        # Show user/assistant turns plus system notices that carry a result.
        shown = [
            m for m in messages
            if m.get("role") in ("user", "assistant") or m.get("result_path")
        ]
        blocks = [
            "<h2 style='margin-bottom:2px;'>💬 AI 对话会话</h2>",
            f"<p style='color:#94a3b8;'>{_fmt_time_relative(record.get('created_at', ''))} · "
            f"{len(shown)} 条消息</p>",
        ]
        for msg in shown:
            content = html.escape(str(msg.get("content", ""))).replace("\n", "<br>")
            result_note = ""
            if msg.get("result_path"):
                result_note = (
                    f"<div style='margin-top:5px;color:#15803d;'>📦 结果图层: "
                    f"{html.escape(str(msg.get('result_layer', '')))}"
                    f"<br><code style='font-size:11px;color:#334155;'>"
                    f"{html.escape(str(msg['result_path']))}</code></div>")
            if msg.get("role") == "user":
                blocks.append(
                    "<div style='background:#eff6ff;border:1px solid #dbeafe;"
                    "border-left:3px solid #3b82f6;border-radius:6px;"
                    "padding:8px 10px;margin:6px 0;font-size:12px;'>"
                    f"<b style='color:#1d4ed8;'>👤 用户</b><br>{content}</div>")
            elif msg.get("role") == "assistant":
                text = content if content else "<span style='color:#94a3b8;'>(工具调用轮次)</span>"
                blocks.append(
                    "<div style='background:#f8fafc;border:1px solid #e2e8f0;"
                    "border-left:3px solid #94a3b8;border-radius:6px;"
                    "padding:8px 10px;margin:6px 0;font-size:12px;'>"
                    f"<b style='color:#475569;'>🤖 助手</b><br>{text}</div>")
            else:
                blocks.append(
                    "<div style='background:#f0fdf4;border:1px solid #bbf7d0;"
                    "border-left:3px solid #16a34a;border-radius:6px;"
                    "padding:8px 10px;margin:6px 0;font-size:12px;'>"
                    f"<b style='color:#15803d;'>📦 运行结果</b><br>{content}{result_note}</div>")
        if not shown:
            blocks.append("<p style='color:#94a3b8;'>（会话暂无消息）</p>")
        self.detail_browser.setHtml("".join(blocks))
        self._set_action_buttons_enabled(True)
        self.load_result_btn.setEnabled(False)
        self.rerun_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _set_action_buttons_enabled(self, enabled: bool):
        for btn in (self.load_result_btn, self.rerun_btn,
                    self.open_folder_btn, self.delete_btn):
            btn.setEnabled(enabled)

    def _on_search_changed(self, _text):
        self._search_timer.start()

    def _on_filter_changed(self):
        self._pending_filter = None
        self.refresh(keep_selection=False)

    def _load_more(self):
        selected = self.record_list.currentItem()
        selected_folder = selected.data(Qt.UserRole) if selected else None
        self.record_list.blockSignals(True)
        self._append_records_page(selected_folder)
        self.record_list.blockSignals(False)

    def _on_load_result(self):
        record = self._selected()
        if not record:
            return
        for out in record.get("outputs", []):
            path = out.get("copied") or out.get("path")
            if path and os.path.isfile(path):
                self._load_layer(path)
                return
        QMessageBox.information(
            self, "提示",
            "该记录没有可加载的结果文件（可能只记录了原始路径且文件已不存在）。")

    def _load_layer(self, path: str):
        from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
        name = f"历史结果_{os.path.basename(path)}"
        if path.lower().endswith(".tif"):
            layer = QgsRasterLayer(path, name)
        else:
            layer = QgsVectorLayer(path, name, "ogr")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            QMessageBox.information(self, "成功", "结果图层已加载到地图。")
        else:
            QMessageBox.warning(self, "提示", "结果文件无法加载为图层。")

    def _on_rerun(self):
        record = self._selected()
        if not record:
            return
        page_key = record.get("page_key", "")
        if record.get("_type") == "copilot":
            self.dock.show_copilot_page()
            self.dock.copilot_page.load_chat_session(record.get("folder", ""))
            return
        if page_key not in self.dock.task_pages:
            QMessageBox.information(self, "提示", "该记录没有关联页面。")
            return
        params = history_store.load_params(record.get("folder", ""))
        self.dock.navigate_to_task(page_key)
        if params:
            self.dock.prefill_page_params(page_key, params)
            QMessageBox.information(
                self, "已预填参数",
                f"已跳转到「{record.get('page_title', page_key)}」并预填该次运行的参数，请确认后执行。")
        else:
            QMessageBox.information(
                self, "已跳转",
                f"已跳转到「{record.get('page_title', page_key)}」，可重新配置后执行。")

    def _on_open_folder(self):
        record = self._selected()
        if record and record.get("folder"):
            history_store.open_folder(record["folder"])

    def _on_delete(self):
        record = self._selected()
        if not record:
            return
        reply = QMessageBox.question(
            self, "删除记录",
            "确定删除这条历史记录及其保存的文件吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        history_store.delete_record(record.get("folder", ""))
        self.refresh(keep_selection=False)

    def _on_clear(self):
        page_key = self.page_filter.currentData() or ""
        scope = "该页面的" if page_key else "全部"
        reply = QMessageBox.question(
            self, "清空历史",
            f"确定清空{scope}历史记录吗？此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        if page_key:
            history_store.clear_page(page_key)
        else:
            history_store.clear_all()
        self.refresh(keep_selection=False)

    # ------------------------------------------------------------------
    # External navigation
    # ------------------------------------------------------------------
    def set_page_filter(self, page_key: str):
        """Jump here pre-filtered for one page (used by per-page 历史 buttons)."""
        self._pending_filter = page_key
        self.refresh(keep_selection=False)


def _status_label(status: str) -> str:
    return {"ok": "运行成功", "failed": "运行失败", "cancelled": "已取消"}.get(status, status)


def _section(title: str, body: str) -> str:
    return (
        "<div style='margin-top:10px;'>"
        f"<div style='font-weight:700; color:#0f172a; font-size:12px; margin-bottom:4px;'>{title}</div>"
        f"<div style='background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; "
        f"padding:8px 10px; font-size:12px; color:#334155;'>{body}</div></div>"
    )
