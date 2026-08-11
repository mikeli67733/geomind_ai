# -*- coding: utf-8 -*-
"""
解译插件停靠面板：
- 顶部标题栏：包含独立右上角 ⚙️ 设置按钮（展开/收起服务器配置）
- 账号区域：用户名密码登录/注册、当前套餐与今日剩余次数展示、升级套餐入口
- 土地利用模型：显示要素多选框
- SAM3 模型：显示 Prompt 提示词输入框
- 使用 QGIS 原生任务框架 (QgsTask/QgsTaskManager) 提交与管理解译任务，
  内置「取消任务」打断机制
兼容 PyQt5 与 PyQt6 (QGIS 3.16 ~ 3.42+)
"""

import os
import tempfile
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QPushButton, QTextEdit, QLineEdit, QProgressBar, QMessageBox,
    QGroupBox, QCheckBox, QApplication, QToolButton, QFrame
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsVectorLayer,
    QgsRectangle, QgsCoordinateTransform, QgsApplication, QgsCoordinateReferenceSystem
)
from qgis.gui import QgsMapLayerComboBox

from .extent_tool import ExtentSelectTool
from .interpret_task import InterpretTask
from .machine_id import get_machine_id
from .auth_client import GeoMindAuthClient, AuthApiError
from .login_dialog import LoginDialog
from .plan_dialog import PlanDialog
from .constants import (
    MODELS, LANDUSE_CLASSES, DEFAULT_CHECKED_CLASS_IDS,
    DEFAULT_SERVER_URL, SETTINGS_ORG, SETTINGS_APP,
    SETTINGS_KEY_SERVER_URL, SETTINGS_KEY_LICENSE_KEY,
    SETTINGS_KEY_TOKEN, SETTINGS_KEY_USERNAME, PLAN_LABELS,
    FREE_PLAN_DAILY_QUOTA,
)

# PyQt5 / PyQt6 图层过滤器枚举兼容处理
RASTER_LAYER_FILTER = getattr(QgsMapLayerProxyModel, 'RasterLayer', None)
if RASTER_LAYER_FILTER is None:
    try:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.RasterLayer
    except AttributeError:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.Raster


class ImageInterpretDockWidget(QDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__("遥感影像智能解译", parent)
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.canvas = iface.mapCanvas()
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        self.extent_tool = None
        self.selected_extent = None
        self.task = None  # 当前正在运行的 InterpretTask (QgsTask)

        self.token = ""
        self.username = ""
        self.account_info = {}

        self._build_ui()
        self._load_settings()
        self._on_model_changed()  # 初始化 UI 显隐状态
        self._try_restore_login()

    # ---------------------------------------------------------------- UI ----
    def _build_ui(self):
        container = QWidget()
        container.setObjectName("dockContainer")
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # 注入现代卡片式 QSS 样式表
        container.setStyleSheet("""
            QWidget#dockContainer {
                background-color: #f8fafc;
            }
            QGroupBox {
                font-weight: bold;
                font-size: 13px;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 14px;
                background-color: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 0 6px;
                color: #334155;
            }
            QPushButton {
                background-color: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 6px 12px;
                color: #334155;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #f1f5f9;
                border-color: #94a3b8;
            }
            QPushButton#runBtn {
                background-color: #2563eb;
                color: #ffffff;
                border: none;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton#runBtn:hover {
                background-color: #1d4ed8;
            }
            QPushButton#runBtn:disabled {
                background-color: #cbd5e1;
                color: #94a3b8;
            }
            QPushButton#cancelBtn {
                background-color: #fef2f2;
                color: #dc2626;
                border: 1px solid #fca5a5;
            }
            QPushButton#cancelBtn:hover {
                background-color: #fee2e2;
            }
            QPushButton#cancelBtn:disabled {
                background-color: #f8fafc;
                color: #cbd5e1;
                border-color: #e2e8f0;
            }
            QToolButton#settingsBtn {
                border: none;
                background: transparent;
                font-size: 15px;
                padding: 4px;
                border-radius: 4px;
            }
            QToolButton#settingsBtn:hover {
                background-color: #e2e8f0;
            }
            QLineEdit, QComboBox, QTextEdit {
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                padding: 5px;
                background-color: #ffffff;
                color: #1e293b;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
                border-color: #2563eb;
            }
            QProgressBar {
                border: none;
                background-color: #e2e8f0;
                border-radius: 4px;
                height: 8px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #2563eb;
                border-radius: 4px;
            }
            QLabel#extentLabel {
                background-color: #f1f5f9;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                padding: 6px;
                font-family: monospace;
                font-size: 11px;
                color: #475569;
            }
        """)

        self.machine_id = get_machine_id()

        # ---------------- 顶部标题栏 + 独立⚙️设置按钮 ----------------
        header_layout = QHBoxLayout()
        header_title = QLabel("AI 遥感智能解译")
        header_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #0f172a;")
        header_layout.addWidget(header_title)
        header_layout.addStretch()

        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("settingsBtn")
        self.settings_btn.setText("⚙️")
        self.settings_btn.setToolTip("服务器网络配置")
        self.settings_btn.clicked.connect(self._toggle_server_settings)
        header_layout.addWidget(self.settings_btn)

        main_layout.addLayout(header_layout)

        # ---------------- 服务器配置（默认隐藏） ----------------
        self.server_group = QGroupBox("服务器配置")
        server_layout = QHBoxLayout()
        server_layout.addWidget(QLabel("服务地址:"))
        self.server_url_edit = QLineEdit()
        self.server_url_edit.setPlaceholderText(DEFAULT_SERVER_URL)
        server_layout.addWidget(self.server_url_edit)
        self.server_group.setLayout(server_layout)
        self.server_group.setVisible(False)  # 点击顶部 ⚙️ 展开/收起
        main_layout.addWidget(self.server_group)

        # ---------------- 0. 账号与套餐 ----------------
        account_group = QGroupBox("账号与套餐")
        account_layout = QVBoxLayout()

        self.account_status_label = QLabel("尚未登录")
        self.account_status_label.setWordWrap(True)
        self.account_status_label.setStyleSheet("color: #64748b;")
        account_layout.addWidget(self.account_status_label)

        self.quota_label = QLabel("")
        self.quota_label.setWordWrap(True)
        self.quota_label.setStyleSheet("color: #334155;")
        account_layout.addWidget(self.quota_label)

        account_btn_row = QHBoxLayout()
        self.login_btn = QPushButton("登录 / 注册")
        self.login_btn.clicked.connect(self._open_login_dialog)
        account_btn_row.addWidget(self.login_btn)

        self.logout_btn = QPushButton("退出登录")
        self.logout_btn.clicked.connect(self._logout)
        self.logout_btn.setVisible(False)
        account_btn_row.addWidget(self.logout_btn)

        self.upgrade_btn = QPushButton("套餐与升级")
        self.upgrade_btn.clicked.connect(self._open_plan_dialog)
        account_btn_row.addWidget(self.upgrade_btn)

        account_layout.addLayout(account_btn_row)
        account_group.setLayout(account_layout)
        main_layout.addWidget(account_group)

        # ---------------- 1. 影像图层选择 ----------------
        layer_group = QGroupBox("1. 选择栅格图层")
        layer_layout = QVBoxLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_layout.addWidget(self.layer_combo)
        layer_group.setLayout(layer_layout)
        main_layout.addWidget(layer_group)

        # ---------------- 2. 选择解译模型 ----------------
        model_group = QGroupBox("2. 选择解译模型")
        model_layout = QVBoxLayout()
        self.model_combo = QComboBox()
        for label, model_key, mode in MODELS:
            self.model_combo.addItem(label, userData={"key": model_key, "mode": mode})
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        model_layout.addWidget(self.model_combo)
        model_group.setLayout(model_layout)
        main_layout.addWidget(model_group)

        # ---------------- 3. 模型参数 (要素类别多选框) ----------------
        self.class_group = QGroupBox("3. 选择要解译的要素类别")
        class_grid = QGridLayout()
        self.class_checkboxes = {}
        for idx, (label, cls_id) in enumerate(LANDUSE_CLASSES):
            cb = QCheckBox(label)
            if cls_id in DEFAULT_CHECKED_CLASS_IDS:
                cb.setChecked(True)
            self.class_checkboxes[cls_id] = cb
            class_grid.addWidget(cb, idx // 3, idx % 3)
        self.class_group.setLayout(class_grid)
        main_layout.addWidget(self.class_group)

        # ---------------- 3. 模型参数 (SAM3 Prompt) ----------------
        self.prompt_group = QGroupBox("3. 输入 SAM 提示词 (Prompt)")
        prompt_layout = QVBoxLayout()
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("例如: water, floodwater (支持英文单词描述)")
        self.prompt_edit.setFixedHeight(65)
        prompt_layout.addWidget(self.prompt_edit)
        self.prompt_group.setLayout(prompt_layout)
        main_layout.addWidget(self.prompt_group)

        # ---------------- 4. 框选范围 ----------------
        extent_group = QGroupBox("4. 框选解译范围")
        extent_layout = QVBoxLayout()

        btn_row = QHBoxLayout()
        self.select_extent_btn = QPushButton("🎯 地图拖拽框选")
        self.select_extent_btn.clicked.connect(self._activate_extent_tool)
        btn_row.addWidget(self.select_extent_btn)

        self.use_canvas_extent_btn = QPushButton("🖼️ 当前视图范围")
        self.use_canvas_extent_btn.clicked.connect(self._use_canvas_extent)
        btn_row.addWidget(self.use_canvas_extent_btn)
        extent_layout.addLayout(btn_row)

        self.extent_label = QLabel("尚未选择解译范围")
        self.extent_label.setObjectName("extentLabel")
        self.extent_label.setWordWrap(True)
        extent_layout.addWidget(self.extent_label)
        extent_group.setLayout(extent_layout)
        main_layout.addWidget(extent_group)

        # ---------------- 5. 执行解译 ----------------
        run_group = QGroupBox("5. 执行任务")
        run_layout = QVBoxLayout()

        run_btn_row = QHBoxLayout()
        self.run_btn = QPushButton("开始智能解译")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_interpret)
        run_btn_row.addWidget(self.run_btn, stretch=2)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.clicked.connect(self._cancel_interpret)
        self.cancel_btn.setEnabled(False)
        run_btn_row.addWidget(self.cancel_btn, stretch=1)
        run_layout.addLayout(run_btn_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        run_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)

        run_group.setLayout(run_layout)
        main_layout.addWidget(run_group)

        main_layout.addStretch()
        container.setLayout(main_layout)
        self.setWidget(container)

    # -------------------------------------------------------- UI 交互逻辑 ----
    def _toggle_server_settings(self):
        """点击右上角 ⚙️ 按钮，切换服务器配置面板显示/隐藏"""
        is_visible = self.server_group.isVisible()
        self.server_group.setVisible(not is_visible)

    def _on_model_changed(self):
        """模型切换时，动态切换显示『多选复选框』或『Prompt输入框』"""
        data = self.model_combo.currentData()
        mode = data.get("mode", "single") if data else "single"

        if mode == "landuse":
            self.class_group.setVisible(True)
            self.prompt_group.setVisible(False)
        elif mode == "sam3":
            self.class_group.setVisible(False)
            self.prompt_group.setVisible(True)
        else:
            self.class_group.setVisible(False)
            self.prompt_group.setVisible(False)


    def _load_settings(self):
        # 每次打开时，重新动态获取最新的远程地址
        from .constants import fetch_remote_server_url
        remote_url = fetch_remote_server_url()

        # 获取本地保存的地址
        saved_url = self.settings.value(SETTINGS_KEY_SERVER_URL, "")

        # 如果本地没有保存过，或者本地地址等于旧的兜底地址，则自动刷新为远程最新地址
        if not saved_url or saved_url == DEFAULT_SERVER_URL:
            self.server_url_edit.setText(remote_url)
        else:
            self.server_url_edit.setText(saved_url)

    def _save_settings(self):
        self.settings.setValue(SETTINGS_KEY_SERVER_URL, self.server_url_edit.text().strip())

    # ------------------------------------------------------------- 账号登录 ---
    def _current_server_url(self) -> str:
        return self.server_url_edit.text().strip() or DEFAULT_SERVER_URL

    def _try_restore_login(self):
        token = self.settings.value(SETTINGS_KEY_TOKEN, "")
        username = self.settings.value(SETTINGS_KEY_USERNAME, "")
        if not token or not username:
            self._update_account_ui()
            return

        self.token = token
        self.username = username
        self._refresh_account_info(silent=True)

    def _open_login_dialog(self):
        dialog = LoginDialog(self._current_server_url, self)
        dialog.loggedIn.connect(self._on_logged_in)
        dialog.exec_()

    def _on_logged_in(self, username: str, token: str):
        self.username = username
        self.token = token
        self.settings.setValue(SETTINGS_KEY_TOKEN, token)
        self.settings.setValue(SETTINGS_KEY_USERNAME, username)
        self._refresh_account_info()

    def _logout(self):
        self.token = ""
        self.username = ""
        self.account_info = {}
        self.settings.remove(SETTINGS_KEY_TOKEN)
        self.settings.remove(SETTINGS_KEY_USERNAME)
        self._update_account_ui()

    def _refresh_account_info(self, silent: bool = False):
        if not self.token:
            self._update_account_ui()
            return

        client = GeoMindAuthClient(self._current_server_url(), self.token)
        try:
            self.account_info = client.get_me()
        except AuthApiError as e:
            self.token = ""
            self.username = ""
            self.account_info = {}
            self.settings.remove(SETTINGS_KEY_TOKEN)
            self.settings.remove(SETTINGS_KEY_USERNAME)
            self._update_account_ui()
            if not silent:
                QMessageBox.warning(self, "提示", f"获取账号信息失败: {e}")
            return

        self._update_account_ui()

    def _update_account_ui(self):
        if not self.token:
            self.account_status_label.setText("尚未登录，请先登录 / 注册后使用解译功能")
            self.account_status_label.setStyleSheet("color: #64748b;")
            self.quota_label.setText("")
            self.login_btn.setVisible(True)
            self.logout_btn.setVisible(False)
            return

        self.login_btn.setVisible(False)
        self.logout_btn.setVisible(True)

        plan = self.account_info.get("plan", "free")
        plan_label = PLAN_LABELS.get(plan, plan)
        self.account_status_label.setText(f"已登录: <b>{self.username}</b>  |  套餐: <b>{plan_label}</b>")
        self.account_status_label.setStyleSheet("color: #15803d;")

        if plan == "free":
            used = self.account_info.get("quota_used_today", 0)
            limit = self.account_info.get("quota_limit_today") or FREE_PLAN_DAILY_QUOTA
            self.quota_label.setText(f"今日剩余免费次数: {max(limit - used, 0)} / {limit}")
        elif plan == "pro":
            expire = self.account_info.get("pro_expire_at", "未知")
            self.quota_label.setText(f"包月会员生效中 (到期: {expire})")
        elif plan == "custom":
            self.quota_label.setText("定制版/私有化部署，不限次数")
        else:
            self.quota_label.setText("")

    def _open_plan_dialog(self):
        if not self.token:
            QMessageBox.information(self, "提示", "请先登录后再查看套餐与升级")
            self._open_login_dialog()
            return

        dialog = PlanDialog(self._current_server_url(), self.token, self.account_info,
                             self.plugin_dir, self)
        dialog.exec_()
        if dialog.account_refreshed:
            self._refresh_account_info(silent=True)

    # ------------------------------------------------------------- 范围选择 ---
    def _activate_extent_tool(self):
        self.extent_tool = ExtentSelectTool(self.canvas)
        self.extent_tool.extentSelected.connect(self._on_extent_selected)
        self.canvas.setMapTool(self.extent_tool)
        self.status_label.setText("请在地图上按住左键拖拽框选范围（右键/Esc取消）")

    def _use_canvas_extent(self):
        rect = self.canvas.extent()
        self._on_extent_selected(rect)

    def _on_extent_selected(self, rect: QgsRectangle):
        self.selected_extent = rect
        crs_id = self.canvas.mapSettings().destinationCrs().authid()
        self.extent_label.setText(
            f"X: [{rect.xMinimum():.2f}, {rect.xMaximum():.2f}]\n"
            f"Y: [{rect.yMinimum():.2f}, {rect.yMaximum():.2f}]\n"
            f"CRS: {crs_id}"
        )
        self.extent_label.setStyleSheet("color: #0f172a;")
        self.status_label.setText("已选定解译范围")

    # --------------------------------------------------------------- 运行 ----
    def _run_interpret(self):
        if self.task is not None:
            QMessageBox.information(self, "提示", "已有任务正在运行，请先取消或等待其完成")
            return

        server_url = self.server_url_edit.text().strip()
        if not server_url:
            QMessageBox.warning(self, "提示", "请填写服务器地址")
            return

        if not self.token:
            QMessageBox.warning(self, "提示", "请先登录账号后再执行解译")
            self._open_login_dialog()
            return

        layer = self.layer_combo.currentLayer()
        if layer is None or not isinstance(layer, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "请选择有效的栅格影像图层")
            return

        if self.selected_extent is None:
            QMessageBox.warning(self, "提示", "请框选解译范围")
            return

        model_data = self.model_combo.currentData()
        model_key = model_data.get("key")
        mode = model_data.get("mode")

        prompt = ""
        target_class = ""

        if mode == "landuse":
            selected_ids = [str(cls_id) for cls_id, cb in self.class_checkboxes.items() if cb.isChecked()]
            if not selected_ids:
                QMessageBox.warning(self, "提示", "请至少勾选一个解译要素类别（如水体、耕地等）")
                return
            target_class = ",".join(selected_ids)
        elif mode == "sam3":
            prompt = self.prompt_edit.toPlainText().strip()
            if not prompt:
                QMessageBox.warning(self, "提示", "使用 SAM3 模型时必须输入提示词 (Prompt)")
                return

        self._save_settings()

        extent_in_layer_crs = self._transform_extent_to_layer_crs(self.selected_extent, layer)

        # 保存画布 CRS 下的原始范围，用于结果加载后裁剪回框选区域
        self._task_canvas_extent = QgsRectangle(self.selected_extent)
        self._task_canvas_crs = self.canvas.mapSettings().destinationCrs()

        self._set_running_state(True)
        self.status_label.setText("正在后台处理，请稍候...")

        task = InterpretTask(
            raster_layer=layer,
            extent=extent_in_layer_crs,
            model_key=model_key,
            target_class=target_class,
            prompt=prompt,
            server_url=server_url,
            license_key="",
            machine_id=self.machine_id,
            token=self.token,
        )
        task.progressMessage.connect(self._on_progress)
        task.taskSucceeded.connect(self._on_finished_ok)
        task.taskFailed.connect(self._on_finished_error)
        task.taskCancelled.connect(self._on_cancelled)

        self.task = task
        QgsApplication.taskManager().addTask(task)

    def _cancel_interpret(self):
        if self.task is None:
            return
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("正在取消任务，请稍候...")
        self.task.cancel()

    def _set_running_state(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.progress_bar.setVisible(running)

    def _transform_extent_to_layer_crs(self, rect, layer):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()
        if canvas_crs == layer_crs:
            return rect
        transform = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
        return transform.transformBoundingBox(rect)

    def _on_progress(self, text):
        self.status_label.setText(text)

    def _on_finished_ok(self, result_path, content_type):
        self._set_running_state(False)
        self.task = None

        time_display = datetime.now().strftime("%H:%M:%S")
        model_label = self.model_combo.currentText().split(' ')[0]
        layer_name = f"解译结果_{model_label}({time_display})"

        if result_path.endswith('.tif'):
            new_layer = QgsRasterLayer(result_path, layer_name)
        else:
            new_layer = QgsVectorLayer(result_path, layer_name, "ogr")

        if not new_layer.isValid():
            QMessageBox.critical(self, "错误", "结果加载失败，返回的文件损坏或格式受限")
            self.status_label.setText("加载结果失败")
            return

        # 将结果裁剪回用户原始框选范围（消除 GDAL 像素对齐 + 服务端分块叠加的溢出）
        canvas_extent = getattr(self, '_task_canvas_extent', None)
        canvas_crs = getattr(self, '_task_canvas_crs', None)
        if canvas_extent and canvas_crs:
            clipped = self._clip_layer_to_extent(
                new_layer, result_path, QgsRectangle(canvas_extent), canvas_crs, layer_name
            )
            if clipped and clipped.isValid():
                new_layer = clipped

        QgsProject.instance().addMapLayer(new_layer)
        self.status_label.setText(f"完成！已加载图层: {layer_name}")
        self._refresh_account_info(silent=True)

    def _on_finished_error(self, error_msg):
        self._set_running_state(False)
        self.task = None
        self.status_label.setText("解译失败")
        QMessageBox.critical(self, "解译失败", error_msg)
        if "登录已过期" in error_msg:
            self._logout()
        elif "免费次数已用完" in error_msg or "402" in error_msg:
            self._open_plan_dialog()

    def _on_cancelled(self):
        self._set_running_state(False)
        self.task = None
        self.status_label.setText("任务已取消")

    def _clip_layer_to_extent(self, layer, result_path, extent, extent_crs, layer_name):
        """将解译结果图层裁剪回用户框选的原始范围"""
        try:
            layer_crs = layer.crs()
            if extent_crs != layer_crs:
                transform = QgsCoordinateTransform(extent_crs, layer_crs, QgsProject.instance())
                extent = transform.transformBoundingBox(extent)
        except Exception as e:
            print(f"[clip] 坐标转换失败，跳过裁剪: {e}")
            return None

        if isinstance(layer, QgsRasterLayer):
            return self._clip_raster_result(result_path, extent, layer_name)
        else:
            return self._clip_vector_result(result_path, extent, layer_name)

    def _clip_raster_result(self, src_path, extent, layer_name):
        """使用 GDAL 将栅格解译结果裁剪到框选范围"""
        try:
            from osgeo import gdal

            out_path = os.path.join(tempfile.gettempdir(), f"clipped_result_{id(self)}.tif")
            proj_win = [
                extent.xMinimum(),
                extent.yMaximum(),
                extent.xMaximum(),
                extent.yMinimum(),
            ]
            options = gdal.TranslateOptions(
                projWin=proj_win,
                creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"],
            )
            ds = gdal.Translate(out_path, src_path, options=options)
            ds = None
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return QgsRasterLayer(out_path, layer_name)
        except Exception as e:
            print(f"[clip] 栅格结果裁剪失败: {e}")
        return None

    def _clip_vector_result(self, src_path, extent, layer_name):
        """使用 QGIS Processing 将矢量解译结果裁剪到框选范围"""
        try:
            from qgis import processing
            result = processing.run("native:extractbyextent", {
                'INPUT': src_path,
                'EXTENT': extent,
                'OUTPUT': 'memory:',
            })
            clipped = result['OUTPUT']
            if clipped and clipped.isValid():
                clipped.setName(layer_name)
                return clipped
        except Exception as e:
            print(f"[clip] 矢量结果裁剪失败: {e}")
        return None

    # --------------------------------------------------------- 生命周期清理 ---
    def cancel_running_task(self):
        if self.task is not None:
            self.task.cancel()

    def closeEvent(self, event):
        self.cancel_running_task()
        super().closeEvent(event)
