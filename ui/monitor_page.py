# -*- coding: utf-8 -*-
"""
7x24 监控助手页 —— 哨兵光学/雷达后台静默分析任务配置与调度界面。
"""
import os

from qgis.PyQt.QtCore import Qt, QDate, QSettings
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QRadioButton, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox,
    QDateEdit, QGroupBox, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QButtonGroup, QDialog, QPlainTextEdit,
)
from qgis.core import QgsRectangle

from ..core.constants import SETTINGS_ORG, SETTINGS_APP
from ..core.monitor_engine import (
    MonitorEngine, default_config, load_job, list_jobs, save_job,
)
from ..core.logger import get_logger
from ..utils.extent_tool import ExtentSelectTool
from ..utils.wechat_push import push_markdown

logger = get_logger("ui.monitor_page")

_SETTINGS_WEBHOOK = "monitor/webhook"
_SETTINGS_WORKDIR = "monitor/workdir"


class MonitorLogDialog(QDialog):
    """7x24 任务运行日志窗口：实时显示后台任务的探测/下载/AI 研判日志。"""

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setWindowTitle("📜 7×24 运行日志")
        self.resize(720, 440)

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("任务:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("全部任务", "")
        self.scope_combo.currentIndexChanged.connect(self._load_current)
        top.addWidget(self.scope_combo, stretch=1)
        self.clear_btn = QPushButton("清空显示")
        self.clear_btn.clicked.connect(lambda: self.view.clear())
        top.addWidget(self.clear_btn)
        lay.addLayout(top)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(3000)
        self.view.setStyleSheet("font-family:Consolas,'Microsoft YaHei UI'; font-size:12px;")
        lay.addWidget(self.view, stretch=1)

        self.engine.logLine.connect(self._on_log_line)
        self._reload_scope()
        self._load_current()

    def showEvent(self, event):
        super().showEvent(event)
        self._reload_scope()
        self._load_current()

    def _reload_scope(self):
        cur = self.scope_combo.currentData()
        self.scope_combo.blockSignals(True)
        self.scope_combo.clear()
        self.scope_combo.addItem("全部任务", "")
        for job in list_jobs():
            self.scope_combo.addItem(job.get("name", job.get("id")), job.get("id"))
        if cur is not None:
            ix = self.scope_combo.findData(cur)
            if ix >= 0:
                self.scope_combo.setCurrentIndex(ix)
        self.scope_combo.blockSignals(False)

    def _on_log_line(self, job_id: str, line: str):
        scope = self.scope_combo.currentData()
        if scope and scope != job_id:
            return
        self.view.appendPlainText(line)

    def _load_current(self):
        self.view.clear()
        scope = self.scope_combo.currentData()
        if scope:
            for ln in self.engine.get_logs(scope):
                self.view.appendPlainText(ln)
            return
        for jid in list(self.engine._logs.keys()):
            for ln in self.engine._logs[jid]:
                self.view.appendPlainText(ln)


class MonitorPage(QWidget):
    """7x24 监控助手主页。"""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self.canvas = main_dock.canvas
        self._selected_extent = None       # QgsRectangle
        self.engine = MonitorEngine(parent=self)
        self._log_dialog = None
        self._build_ui()
        self._load_persisted()
        self.engine.jobsChanged.connect(self._refresh_list)
        self._refresh_list()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # ---- 任务配置卡片 ----
        cfg_group = QGroupBox("新建后台静默监控任务")
        cfg_group.setStyleSheet("QGroupBox::title { top: 18px; }")
        cfg_layout = QVBoxLayout(cfg_group)
        cfg_layout.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("数据源:"))
        self.rb_s2 = QRadioButton("哨兵光学 (Sentinel-2)")
        self.rb_s1 = QRadioButton("哨兵雷达 (Sentinel-1)")
        self.rb_s2.setChecked(True)
        self.rb_s2.toggled.connect(self._on_source_changed)
        row.addWidget(self.rb_s2)
        row.addWidget(self.rb_s1)
        row.addWidget(QLabel("极化:"))
        self.pol_combo = QComboBox()
        self.pol_combo.addItems(["vv", "vh", "hh", "hv"])
        self.pol_combo.setEnabled(False)
        row.addWidget(self.pol_combo)
        row.addStretch()
        cfg_layout.addLayout(row)

        row = QHBoxLayout()
        self.extent_btn = QPushButton("🐾 框选监控区域")
        self.extent_btn.clicked.connect(self._start_extent_select)
        row.addWidget(self.extent_btn)
        self.extent_label = QLabel("尚未框选区域")
        self.extent_label.setObjectName("extentLabel")
        self.extent_label.setWordWrap(True)
        row.addWidget(self.extent_label, stretch=1)
        cfg_layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("任务模式:"))
        self.rb_watch = QRadioButton("从现在开始（立即处理最新景，随后静默监测）")
        self.rb_backfill = QRadioButton("历史回补（完成后自动停止）")
        self.rb_watch.setChecked(True)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate().addMonths(-1))
        self.date_edit.setEnabled(False)
        self.rb_watch.toggled.connect(
            lambda on: self.date_edit.setEnabled(not on))
        row.addWidget(self.rb_watch)
        row.addWidget(self.rb_backfill)
        row.addWidget(self.date_edit)
        row.addStretch()
        cfg_layout.addLayout(row)

        self._src_group = QButtonGroup(self)
        self._src_group.addButton(self.rb_s2)
        self._src_group.addButton(self.rb_s1)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.rb_watch)
        self._mode_group.addButton(self.rb_backfill)
        for _rb in (self.rb_s2, self.rb_s1, self.rb_watch, self.rb_backfill):
            _rb.setAutoExclusive(False)

        row = QHBoxLayout()
        row.addWidget(QLabel("检测内容:"))
        self.prompt_edit = QLineEdit()
        self.prompt_edit.setPlaceholderText(
            "自然语言描述。如：土壤水分监测；水体与植被长势变化；形变监测（雷达）…")
        row.addWidget(self.prompt_edit, stretch=1)
        cfg_layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("目标阈值:"))
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(-1.0, 1.0)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setValue(0.0)
        self.threshold_spin.setDecimals(3)
        row.addWidget(self.threshold_spin)
        self.threshold_hint = QLabel("光学: ≥阈值=目标区；雷达: |Δ/基准|≥阈值=变化")
        self.threshold_hint.setStyleSheet("color:#94a3b8; font-size:11px;")
        row.addWidget(self.threshold_hint, stretch=1)
        cfg_layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("运行方式:"))
        self.rb_foreground = QRadioButton("前台 · 结果加载到地图")
        self.rb_silent = QRadioButton("后台静默 · 不加载图层")
        self.rb_foreground.setChecked(True)
        self.rb_silent.setToolTip("只在后台拉取→分析→存工作文件夹→企业微信推送，不加载图层、不打扰地图")
        row.addWidget(self.rb_foreground)
        row.addWidget(self.rb_silent)
        row.addStretch()
        cfg_layout.addLayout(row)
        self._run_group = QButtonGroup(self)
        self._run_group.addButton(self.rb_foreground)
        self._run_group.addButton(self.rb_silent)
        for _rb in (self.rb_foreground, self.rb_silent):
            _rb.setAutoExclusive(False)

        row = QHBoxLayout()
        row.addWidget(QLabel("心跳(分钟):"))
        self.heartbeat_spin = QSpinBox()
        self.heartbeat_spin.setRange(1, 1440)
        self.heartbeat_spin.setValue(30)
        row.addWidget(self.heartbeat_spin)
        row.addWidget(QLabel("工作文件夹:"))
        self.workdir_edit = QLineEdit()
        self.workdir_edit.setPlaceholderText("结果输出目录（留空自动使用插件缓存区）")
        row.addWidget(self.workdir_edit, stretch=1)
        self.browse_btn = QPushButton("…")
        self.browse_btn.setFixedWidth(30)
        self.browse_btn.clicked.connect(self._browse_workdir)
        row.addWidget(self.browse_btn)
        cfg_layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("通信方式:"))
        self.comm_combo = QComboBox()
        self.comm_combo.addItem("企业微信（默认）")
        row.addWidget(self.comm_combo)
        self.webhook_edit = QLineEdit()
        self.webhook_edit.setPlaceholderText("企业微信群机器人 Webhook（可留空）")
        row.addWidget(self.webhook_edit, stretch=1)
        self.test_push_btn = QPushButton("测试推送")
        self.test_push_btn.clicked.connect(self._test_push)
        row.addWidget(self.test_push_btn)
        cfg_layout.addLayout(row)

        btn_row = QHBoxLayout()
        self.create_btn = QPushButton("▶ 启动后台任务")
        self.create_btn.setObjectName("runBtn")
        self.create_btn.clicked.connect(self._create_and_start)
        btn_row.addWidget(self.create_btn)
        btn_row.addStretch()
        cfg_layout.addLayout(btn_row)

        layout.addWidget(cfg_group)

        # ---- 任务列表 ----
        list_group = QGroupBox("后台监控列表")
        list_group.setStyleSheet("QGroupBox::title { top: 18px; }")
        list_layout = QVBoxLayout(list_group)
        self.job_list = QListWidget()
        self.job_list.currentItemChanged.connect(self._on_select_job)
        list_layout.addWidget(self.job_list, stretch=1)

        self.detail_label = QLabel("（选中任务后显示状态与最近结果）")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color:#475569; font-size:12px;")
        list_layout.addWidget(self.detail_label)

        act_row = QHBoxLayout()
        self.pause_btn = QPushButton("⏸ 暂停 / ▶ 继续")
        self.pause_btn.clicked.connect(self._toggle_pause)
        act_row.addWidget(self.pause_btn)
        self.log_btn = QPushButton("📜 运行日志")
        self.log_btn.setToolTip("打开任务运行日志窗口（实时显示探测/下载/AI 研判）")
        self.log_btn.clicked.connect(self._open_log)
        act_row.addWidget(self.log_btn)
        self.open_dir_btn = QPushButton("📁 打开输出目录")
        self.open_dir_btn.clicked.connect(self._open_workdir)
        act_row.addWidget(self.open_dir_btn)
        self.delete_btn = QPushButton("🗑 删除任务")
        self.delete_btn.setObjectName("cancelBtn")
        self.delete_btn.clicked.connect(self._delete_job)
        act_row.addWidget(self.delete_btn)
        act_row.addStretch()
        list_layout.addLayout(act_row)

        layout.addWidget(list_group, stretch=1)

        tip = QLabel("💡 后台静默运行，不加载图层、不打扰地图，只在后台拉取→分析→存工作文件夹→推送。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#64748b; font-size:11px;")
        layout.addWidget(tip)

    def _load_persisted(self):
        s = QSettings(SETTINGS_ORG, SETTINGS_APP)
        wh = s.value(_SETTINGS_WEBHOOK, "")
        if wh:
            self.webhook_edit.setText(str(wh))
        wd = s.value(_SETTINGS_WORKDIR, "")
        if wd:
            self.workdir_edit.setText(str(wd))

    def _on_source_changed(self):
        is_s1 = self.rb_s1.isChecked()
        self.pol_combo.setEnabled(is_s1)
        if is_s1:
            self.threshold_spin.setValue(0.15)
            self.threshold_hint.setText("雷达: |Δ/基准|≥阈值 = 变化")
        else:
            self.threshold_spin.setValue(0.0)
            self.threshold_hint.setText("光学: ≥阈值 = 目标区")

    def _start_extent_select(self):
        tool = ExtentSelectTool(self.canvas)
        tool.extentSelected.connect(self._on_extent_selected)
        self.canvas.setMapTool(tool)
        self.extent_label.setText("请在地图上框选监控范围...")

    def _on_extent_selected(self, rect: QgsRectangle):
        self._selected_extent = QgsRectangle(rect)
        crs_id = self.canvas.mapSettings().destinationCrs().authid()
        self.extent_label.setText(
            f"X[{rect.xMinimum():.2f}–{rect.xMaximum():.2f}] "
            f"Y[{rect.yMinimum():.2f}–{rect.yMaximum():.2f}] ({crs_id})")

    def _browse_workdir(self):
        path = QFileDialog.getExistingDirectory(self, "选择工作文件夹",
                                                self.workdir_edit.text() or "")
        if path:
            self.workdir_edit.setText(path)

    def _collect_config(self) -> dict:
        cfg = default_config()
        prompt = self.prompt_edit.text().strip()
        tag = (prompt or "监测")[:14]
        is_fg = self.rb_foreground.isChecked()
        cfg["load_map"] = is_fg
        if self.rb_s2.isChecked():
            cfg["name"] = ("前台-光学-" if is_fg else "静默-光学-") + tag
        else:
            cfg["name"] = (f"前台-雷达{self.pol_combo.currentText()}-"
                           if is_fg else
                           f"静默-雷达{self.pol_combo.currentText()}-") + tag
        cfg["source"] = "s2" if self.rb_s2.isChecked() else "s1"
        cfg["pol"] = self.pol_combo.currentText()
        cfg["mode"] = "watch" if self.rb_watch.isChecked() else "backfill"
        if cfg["mode"] == "backfill":
            cfg["start_date"] = self.date_edit.date().toString("yyyy-MM-dd")
        cfg["prompt"] = prompt
        cfg["threshold"] = self.threshold_spin.value()
        cfg["heartbeat_min"] = self.heartbeat_spin.value()
        cfg["work_dir"] = self.workdir_edit.text().strip()
        cfg["webhook"] = self.webhook_edit.text().strip()
        if self._selected_extent is not None:
            r = self._selected_extent
            cfg["extent"] = [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()]
            # 优先从 canvas 获取当前投影代码（例如 EPSG:3857）
            canvas_crs = self.canvas.mapSettings().destinationCrs().authid()
            cfg["extent_crs"] = canvas_crs or "EPSG:3857"
        return cfg

    def _create_and_start(self):
        try:
            cfg = self._collect_config()
            if not cfg["extent"]:
                QMessageBox.warning(self, "提示", "请先在地图上框选监控区域")
                return
            if cfg["mode"] == "backfill" and cfg["start_date"] >= \
                    QDate.currentDate().toString("yyyy-MM-dd"):
                QMessageBox.warning(self, "提示", "回补起始日期必须早于今天")
                return
            job_id = self.engine.create_job(cfg)

            s = QSettings(SETTINGS_ORG, SETTINGS_APP)
            if cfg["webhook"]:
                s.setValue(_SETTINGS_WEBHOOK, cfg["webhook"])
            if cfg["work_dir"]:
                s.setValue(_SETTINGS_WORKDIR, cfg["work_dir"])

            self.engine.start_job(job_id)
            self._refresh_list()
            for i in range(self.job_list.count()):
                if self.job_list.item(i).data(Qt.UserRole) == job_id:
                    self.job_list.setCurrentRow(i)
                    break
            self.detail_label.setText("▶ 任务已在后台启动，首次执行立即获取最新一景影像...")
        except Exception as exc:
            logger.exception("启动任务异常")
            QMessageBox.critical(self, "启动失败", f"任务启动出错：{exc}")

    def _selected_job_id(self):
        item = self.job_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _refresh_list(self):
        self.job_list.blockSignals(True)
        self.job_list.clear()
        for job in list_jobs():
            st = job["state"]
            status = "▶ 运行中" if st["status"] == "running" else "⏸ 暂停"
            mode = "前台" if job.get("load_map") else "静默"
            cursor = st.get("cursor_date") or "—"
            text = (f"{job.get('name', job.get('id'))} | {status} | {mode} | "
                    f"最新景: {cursor} | 心跳 {job.get('heartbeat_min')}分")
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, job.get("id"))
            self.job_list.addItem(item)
        self.job_list.blockSignals(False)
        if self.job_list.count() and not self.job_list.currentItem():
            self.job_list.setCurrentRow(0)
        self._on_select_job(self.job_list.currentItem(), None)

    def _on_select_job(self, current, _previous):
        job_id = self._selected_job_id()
        if not job_id:
            self.detail_label.setText("（选中任务后显示状态与最近结果）")
            return
        job = load_job(job_id)
        if not job:
            return
        st = job["state"]
        lines = [
            f"状态: {'▶ 后台运行中' if st['status'] == 'running' else '⏸ 暂停'} | "
            f"最近检查: {st.get('last_check_at') or '—'}",
        ]
        if st.get("cursor_scene"):
            lines.append(f"已处理最新景: {st.get('cursor_scene')} ({st.get('cursor_date')})")
        if st.get("last_result"):
            lines.append("最近状态: " + str(st["last_result"])[:200])
        if st.get("last_error"):
            lines.append(f"⚠️ 异常: {st['last_error']}")
        lines.append(f"输出历史: {len(st.get('outputs', []))} 次")
        self.detail_label.setText("\n".join(lines))

    def _toggle_pause(self):
        job_id = self._selected_job_id()
        if not job_id:
            return
        job = load_job(job_id)
        if job and job["state"]["status"] == "running":
            self.engine.pause_job(job_id)
        else:
            self.engine.start_job(job_id)
        self._refresh_list()

    def _check_now(self):
        job_id = self._selected_job_id()
        if not job_id:
            QMessageBox.information(self, "提示", "请先选择一个任务")
            return
        msg = self.engine.run_now(job_id)
        self.detail_label.setText(msg[:300])

    def _open_log(self):
        """打开（或唤起）运行日志窗口，非模态，可边运行边看。"""
        if self._log_dialog is None:
            self._log_dialog = MonitorLogDialog(self.engine, self)
        self._log_dialog._reload_scope()
        self._log_dialog.show()
        self._log_dialog.raise_()
        self._log_dialog.activateWindow()

    def _open_workdir(self):
        job_id = self._selected_job_id()
        if not job_id:
            return
        job = load_job(job_id)
        work = (job or {}).get("work_dir")
        if work and os.path.isdir(work):
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(work))

    def _delete_job(self):
        job_id = self._selected_job_id()
        if not job_id:
            return
        reply = QMessageBox.question(
            self, "删除任务", "确定删除该后台任务吗？(已保存文件保留)",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.engine.delete_job(job_id)
            self._refresh_list()

    def _test_push(self):
        url = self.webhook_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请先填写 Webhook URL")
            return
        err = push_markdown(url, "✅ **GeoMind AI 静默监控助手连接正常**")
        if err:
            QMessageBox.critical(self, "推送失败", err)
        else:
            QMessageBox.information(self, "成功", "推送测试成功！")

    def restore_jobs(self):
        n = self.engine.restore()
        logger.info("后台监控任务恢复: %d 个", n)
        self._refresh_list()