# -*- coding: utf-8 -*-
"""
遥感解译插件停靠面板 (DockWidget)
支持功能：
- 顶部设置：服务器网关地址配置与动态刷新 ⚙️
- 账号与套餐：JWT 登录/注册、每日额度实时显示
- 动态模型切换：
    1. 土地利用全要素解译 (多类要素复选框)
    2. SAM3 交互式大模型 (Prompt 提示词输入 + 分割/检测框切换)
    3. 双期影像变化检测 (支持 T1 基准期 + T2 变化期双图选择)
    4. 目标检测: 电力铁塔 / 光伏电站 / 通用目标 (单图框选提取)
- QGIS 原生任务框架 (QgsTask/QgsTaskManager) 异步提交与中途打断
兼容 PyQt5 与 PyQt6 (QGIS 3.16 ~ 3.34+)
"""

import os
import tempfile
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QPushButton, QTextEdit, QLineEdit, QProgressBar, QMessageBox,
    QGroupBox, QCheckBox, QApplication, QToolButton
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsVectorLayer,
    QgsRectangle, QgsCoordinateTransform, QgsApplication
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
    SETTINGS_KEY_SERVER_URL, SETTINGS_KEY_TOKEN, SETTINGS_KEY_USERNAME,
    PLAN_LABELS, FREE_PLAN_DAILY_QUOTA, fetch_remote_server_url
)

# PyQt5 / PyQt6 栅格图层过滤器枚举兼容处理
RASTER_LAYER_FILTER = getattr(QgsMapLayerProxyModel, 'RasterLayer', None)
if RASTER_LAYER_FILTER is None:
    try:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.RasterLayer
    except AttributeError:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.Raster


class ImageInterpretDockWidget(QDockWidget):

    def __init__(self, iface, parent=None):
        super().__init__("GeoAI 遥感智能解译", parent)
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.canvas = iface.mapCanvas()
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

        self.extent_tool = None
        self.selected_extent = None
        self.task = None  # 当前运行的 QgsTask

        self.token = ""
        self.username = ""
        self.account_info = {}

        self._build_ui()
        self._load_settings()
        self._on_model_changed()  # 初始化 UI 显隐状态
        self._try_restore_login()

    # =========================================================================
    # 1. UI 界面构建
    # =========================================================================
    def _build_ui(self):
        container = QWidget()
        container.setObjectName("dockContainer")
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # QSS 现代样式表
        container.setStyleSheet("""
            QWidget#dockContainer { background-color: #f8fafc; }
            QGroupBox {
                font-weight: bold; font-size: 13px; border: 1px solid #e2e8f0;
                border-radius: 8px; margin-top: 8px; padding-top: 14px; background-color: #ffffff;
            }
            QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 6px; color: #334155; }
            QPushButton { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 12px; color: #334155; font-weight: 500; }
            QPushButton:hover { background-color: #f1f5f9; border-color: #94a3b8; }
            QPushButton#runBtn { background-color: #2563eb; color: #ffffff; border: none; font-size: 13px; font-weight: bold; }
            QPushButton#runBtn:hover { background-color: #1d4ed8; }
            QPushButton#runBtn:disabled { background-color: #cbd5e1; color: #94a3b8; }
            QPushButton#cancelBtn { background-color: #fef2f2; color: #dc2626; border: 1px solid #fca5a5; }
            QPushButton#cancelBtn:hover { background-color: #fee2e2; }
            QToolButton#settingsBtn { border: none; background: transparent; font-size: 15px; padding: 4px; border-radius: 4px; }
            QToolButton#settingsBtn:hover { background-color: #e2e8f0; }
            QLineEdit, QComboBox, QTextEdit { border: 1px solid #cbd5e1; border-radius: 6px; padding: 5px; background-color: #ffffff; color: #1e293b; }
            QProgressBar { border: none; background-color: #e2e8f0; border-radius: 4px; height: 8px; text-align: center; }
            QProgressBar::chunk { background-color: #2563eb; border-radius: 4px; }
            QLabel#extentLabel { background-color: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 6px; padding: 6px; font-family: monospace; font-size: 11px; color: #475569; }
            QLabel#noticeBanner { background-color: #f0f9ff; border: 1px solid #bae6fd; border-radius: 6px; padding: 6px 8px; color: #0369a1; font-size: 11px; }
        """)

        self.machine_id = get_machine_id()

        # ---------------- 顶部标题栏 + ⚙️设置按钮 ----------------
        header_layout = QHBoxLayout()
        header_title = QLabel("AI 遥感智能解译终端")
        header_title.setStyleSheet("font-size: 15px; font-weight: bold; color: #0f172a;")
        header_layout.addWidget(header_title)
        header_layout.addStretch()

        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("settingsBtn")
        self.settings_btn.setText("⚙️")
        self.settings_btn.setToolTip("网络与网关服务器配置")
        self.settings_btn.clicked.connect(self._toggle_server_settings)
        header_layout.addWidget(self.settings_btn)

        main_layout.addLayout(header_layout)

        # ---------------- 服务器配置 (默认折叠) ----------------
        self.server_group = QGroupBox("服务器配置")
        server_vlayout = QVBoxLayout()

        server_layout = QHBoxLayout()
        server_layout.addWidget(QLabel("网关地址:"))
        self.server_url_edit = QLineEdit()
        self.server_url_edit.setPlaceholderText(DEFAULT_SERVER_URL)
        server_layout.addWidget(self.server_url_edit)

        self.refresh_url_btn = QPushButton("🔄 刷新")
        self.refresh_url_btn.setToolTip("从远程获取最新的网关通道")
        self.refresh_url_btn.clicked.connect(self._refresh_server_url)
        server_layout.addWidget(self.refresh_url_btn)

        server_vlayout.addLayout(server_layout)
        self.server_group.setLayout(server_vlayout)
        self.server_group.setVisible(False)
        main_layout.addWidget(self.server_group)

        # ---------------- 0. 账号与套餐 ----------------
        account_group = QGroupBox("账号与套餐")
        account_layout = QVBoxLayout()

        self.notice_banner = QLabel("💡 提示：若网络异常或提交失败，点击右上角 ⚙️ 展开设置并点击【🔄 刷新】按钮同步网关。")
        self.notice_banner.setObjectName("noticeBanner")
        self.notice_banner.setWordWrap(True)
        account_layout.addWidget(self.notice_banner)

        self.account_status_label = QLabel("尚未登录")
        self.account_status_label.setStyleSheet("color: #64748b;")
        account_layout.addWidget(self.account_status_label)

        self.quota_label = QLabel("")
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

        # ---------------- 1. 栅格图层选择 ----------------
        layer_group = QGroupBox("1. 选择输入栅格图层")
        layer_layout = QVBoxLayout()

        # 基准期 T1
        self.lbl_t1 = QLabel("基准期 / 目标影像 (T1):")
        self.layer_combo_t1 = QgsMapLayerComboBox()
        self.layer_combo_t1.setFilters(RASTER_LAYER_FILTER)
        layer_layout.addWidget(self.lbl_t1)
        layer_layout.addWidget(self.layer_combo_t1)

        # 变化期 T2 (专供变化检测，动态显隐)
        self.lbl_t2 = QLabel("变化期影像 (T2):")
        self.layer_combo_t2 = QgsMapLayerComboBox()
        self.layer_combo_t2.setFilters(RASTER_LAYER_FILTER)
        layer_layout.addWidget(self.lbl_t2)
        layer_layout.addWidget(self.layer_combo_t2)

        layer_group.setLayout(layer_layout)
        main_layout.addWidget(layer_group)

        # ---------------- 2. 模型选择 ----------------
        model_group = QGroupBox("2. 选择 AI 解译模型")
        model_layout = QVBoxLayout()
        self.model_combo = QComboBox()
        for label, model_key, mode in MODELS:
            self.model_combo.addItem(label, userData={"key": model_key, "mode": mode})
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        model_layout.addWidget(self.model_combo)
        model_group.setLayout(model_layout)
        main_layout.addWidget(model_group)

        # ---------------- 3. 参数区: 要素类别多选框 (土地利用) ----------------
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

        # ---------------- 3. 参数区: SAM3 提示词 (Prompt) ----------------
        self.prompt_group = QGroupBox("3. 输入 SAM 提示词 (Prompt)")
        prompt_layout = QVBoxLayout()
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("例如: water, building (英文单词描述)")
        self.prompt_edit.setFixedHeight(50)
        prompt_layout.addWidget(self.prompt_edit)

        # 🚨【新增】：SAM3 输出类型切换 (分割图斑 vs 检测方框)
        sam_out_layout = QHBoxLayout()
        sam_out_layout.addWidget(QLabel("输出形式:"))
        self.sam_out_type_combo = QComboBox()
        self.sam_out_type_combo.addItem("矢量分割图斑 (Polygon)", "mask")
        self.sam_out_type_combo.addItem("目标检测方框 (Bounding Box)", "bbox")
        sam_out_layout.addWidget(self.sam_out_type_combo)
        prompt_layout.addLayout(sam_out_layout)

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

    # =========================================================================
    # 2. UI 动态联动与逻辑控制
    # =========================================================================
    def _toggle_server_settings(self):
        self.server_group.setVisible(not self.server_group.isVisible())

    def _refresh_server_url(self):
        self.refresh_url_btn.setEnabled(False)
        self.refresh_url_btn.setText("刷新中...")
        QApplication.processEvents()
        try:
            new_url = fetch_remote_server_url()
            if new_url:
                self.server_url_edit.setText(new_url)
                self.settings.remove(SETTINGS_KEY_SERVER_URL)
                self.settings.setValue("is_custom_server", False)
                QMessageBox.information(self, "成功", f"成功获取最新在线网关地址：\n{new_url}")
            else:
                QMessageBox.warning(self, "提示", "未能获取最新地址，请检查网络")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"刷新网关地址失败: {e}")
        finally:
            self.refresh_url_btn.setEnabled(True)
            self.refresh_url_btn.setText("🔄 刷新")

    def _on_model_changed(self):
        """根据模型类型，动态切换组件的显隐状态"""
        data = self.model_combo.currentData()
        mode = data.get("mode", "landuse") if data else "landuse"

        # 1. T2 图层选择框：仅在“变化检测”时显示
        if mode == "change_detection":
            self.lbl_t2.setVisible(True)
            self.layer_combo_t2.setVisible(True)
        else:
            self.lbl_t2.setVisible(False)
            self.layer_combo_t2.setVisible(False)

        # 2. 参数选择区域联动
        if mode == "landuse":
            self.class_group.setVisible(True)
            self.prompt_group.setVisible(False)
        elif mode == "sam3":
            self.class_group.setVisible(False)
            self.prompt_group.setVisible(True)
        else: # detection 或 change_detection
            self.class_group.setVisible(False)
            self.prompt_group.setVisible(False)

    def showEvent(self, event):
        super().showEvent(event)
        if self.token:
            self._refresh_account_info(silent=True)

    def _load_settings(self):
        remote_url = fetch_remote_server_url()
        is_custom = self.settings.value("is_custom_server", False, type=bool)
        saved_url = self.settings.value(SETTINGS_KEY_SERVER_URL, "")

        if is_custom and saved_url:
            self.server_url_edit.setText(saved_url)
        else:
            target_url = remote_url or DEFAULT_SERVER_URL
            self.server_url_edit.setText(target_url)
            self.settings.remove(SETTINGS_KEY_SERVER_URL)
            self.settings.setValue("is_custom_server", False)

    def _save_settings(self):
        current_url = self.server_url_edit.text().strip()
        remote_url = fetch_remote_server_url()

        if current_url and current_url != remote_url:
            self.settings.setValue(SETTINGS_KEY_SERVER_URL, current_url)
            self.settings.setValue("is_custom_server", True)
        else:
            self.settings.remove(SETTINGS_KEY_SERVER_URL)
            self.settings.setValue("is_custom_server", False)

    # =========================================================================
    # 3. 账号与登录
    # =========================================================================
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
                QMessageBox.warning(self, "提示", f"获取账号信息失败: {e}\n\n💡 提示：可尝试点击 ⚙️ 设置里的【🔄 刷新】按钮。")
            return

        self._update_account_ui()

    def _update_account_ui(self):
        if not self.token:
            self.account_status_label.setText("尚未登录，请先登录后使用解译功能")
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
            self.quota_label.setText(f"今日剩余解译次数: {max(limit - used, 0)} / {limit}")
        elif plan == "pro":
            expire = self.account_info.get("pro_expire_at", "未知")
            self.quota_label.setText(f"包月会员生效中 (到期: {expire})")
        else:
            self.quota_label.setText("定制版本，无限制")

    def _open_plan_dialog(self):
        if not self.token:
            QMessageBox.information(self, "提示", "请先登录后再查看套餐")
            self._open_login_dialog()
            return

        dialog = PlanDialog(self._current_server_url(), self.token, self.account_info, self.plugin_dir, self)
        dialog.exec_()
        if dialog.account_refreshed:
            self._refresh_account_info(silent=True)

    # =========================================================================
    # 4. 范围框选
    # =========================================================================
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
        self.status_label.setText("已选定解译范围")

    # =========================================================================
    # 5. 任务提交与执行
    # =========================================================================
    def _run_interpret(self):
        if self.task is not None:
            QMessageBox.information(self, "提示", "已有任务正在运行，请先取消或等待其完成")
            return

        server_url = self.server_url_edit.text().strip()
        if not server_url:
            QMessageBox.warning(self, "提示", "请填写网关服务器地址")
            return

        if not self.token:
            QMessageBox.warning(self, "提示", "请先登录账号后再执行解译")
            self._open_login_dialog()
            return

        layer_t1 = self.layer_combo_t1.currentLayer()
        if layer_t1 is None or not isinstance(layer_t1, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "请选择有效的基准期 T1 栅格图层")
            return

        if self.selected_extent is None:
            QMessageBox.warning(self, "提示", "请先框选解译范围")
            return

        model_data = self.model_combo.currentData()
        model_key = model_data.get("key")
        mode = model_data.get("mode")

        layer_t2 = None
        prompt = ""
        target_class = ""
        output_format = "mask"  # 默认输出为分割矢量图斑

        # 校验不同模式参数
        if mode == "change_detection":
            layer_t2 = self.layer_combo_t2.currentLayer()
            if layer_t2 is None or not isinstance(layer_t2, QgsRasterLayer):
                QMessageBox.warning(self, "提示", "变化检测模式下，必须同时选择后期 T2 栅格图层")
                return
        elif mode == "landuse":
            selected_ids = [str(cls_id) for cls_id, cb in self.class_checkboxes.items() if cb.isChecked()]
            if not selected_ids:
                QMessageBox.warning(self, "提示", "请至少勾选一个解译要素类别")
                return
            target_class = ",".join(selected_ids)
        elif mode == "sam3":
            prompt = self.prompt_edit.toPlainText().strip()
            if not prompt:
                QMessageBox.warning(self, "提示", "使用 SAM3 模型必须输入提示词 (Prompt)")
                return
            # 🚨 获取选择的 SAM3 输出类型 ('mask' 或 'bbox')
            output_format = self.sam_out_type_combo.currentData() or "mask"

        self._save_settings()

        canvas_crs = self.canvas.mapSettings().destinationCrs()

        self._task_canvas_extent = QgsRectangle(self.selected_extent)
        self._task_canvas_crs = canvas_crs

        self._set_running_state(True)
        self.status_label.setText("正在打包影像并提交任务，请稍候...")

        # 启动后台解译任务，透传 output_format
        task = InterpretTask(
            raster_layer=layer_t1,
            raster_layer_after=layer_t2,
            extent=self.selected_extent,
            extent_crs=canvas_crs,
            model_key=model_key,
            target_class=target_class,
            prompt=prompt,
            output_format=output_format,  # 👈 传入输出类型 (mask / bbox)
            server_url=server_url,
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
        if self.task is None: return
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("正在打断任务，请稍候...")
        self.task.cancel()

    def _set_running_state(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.progress_bar.setVisible(running)

    def _transform_extent_to_layer_crs(self, rect, layer):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()
        if canvas_crs == layer_crs: return rect
        transform = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
        return transform.transformBoundingBox(rect)

    def _on_progress(self, text):
        self.status_label.setText(text)

    # 6. 插件卸载/重新加载时的清理钩子 (供 plugin_main.py 调用)
    # =========================================================================
    def cancel_running_task(self):
        """取消当前正在运行的任务（供 plugin_main.py 在 unload 卸载插件时调用）"""
        if self.task is not None:
            try:
                self._cancel_interpret()
            except Exception as e:
                print(f"卸载插件打断任务异常: {e}")

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
            QMessageBox.critical(self, "错误", "解译结果加载失败，文件损坏或格式受限")
            self.status_label.setText("加载结果失败")
            return

        # 裁切回原始框选范围
        canvas_extent = getattr(self, '_task_canvas_extent', None)
        canvas_crs = getattr(self, '_task_canvas_crs', None)
        if canvas_extent and canvas_crs:
            clipped = self._clip_layer_to_extent(
                new_layer, result_path, QgsRectangle(canvas_extent), canvas_crs, layer_name
            )
            if clipped and clipped.isValid():
                new_layer = clipped

        QgsProject.instance().addMapLayer(new_layer)
        self.status_label.setText(f"解译完成！已成功加载图层: {layer_name}")
        self._refresh_account_info(silent=True)

    def _on_finished_error(self, error_msg):
        self._set_running_state(False)
        self.task = None
        self.status_label.setText("解译失败")
        QMessageBox.critical(self, "解译失败", f"{error_msg}\n\n💡 提示：若遇到网关错误，可尝试点击 ⚙️ 设置中的【🔄 刷新】按钮。")

        if "登录已过期" in error_msg:
            self._logout()
        elif "免费次数已用完" in error_msg or "402" in error_msg:
            self._open_plan_dialog()

    def _on_cancelled(self):
        self._set_running_state(False)
        self.task = None
        self.status_label.setText("任务已成功打断")

    def _clip_layer_to_extent(self, layer, result_path, extent, extent_crs, layer_name):
        """将解译结果矢量/栅格精准裁切回用户框选的范围"""
        try:
            layer_crs = layer.crs()
            if extent_crs != layer_crs:
                transform = QgsCoordinateTransform(extent_crs, layer_crs, QgsProject.instance())
                extent = transform.transformBoundingBox(extent)
        except Exception as e:
            print(f"[clip] 坐标转换异常: {e}")
            return None

        if isinstance(layer, QgsRasterLayer):
            try:
                from osgeo import gdal
                out_path = os.path.join(tempfile.gettempdir(), f"clipped_{id(self)}.tif")
                proj_win = [extent.xMinimum(), extent.yMaximum(), extent.xMaximum(), extent.yMinimum()]
                ds = gdal.Translate(out_path, result_path, options=gdal.TranslateOptions(projWin=proj_win))
                ds = None
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return QgsRasterLayer(out_path, layer_name)
            except Exception as e:
                print(f"[clip] 栅格裁切失败: {e}")
        else:
            try:
                from qgis import processing
                result = processing.run("native:extractbyextent", {
                    'INPUT': result_path, 'EXTENT': extent, 'OUTPUT': 'memory:'
                })
                clipped = result['OUTPUT']
                if clipped and clipped.isValid():
                    clipped.setName(layer_name)
                    return clipped
            except Exception as e:
                print(f"[clip] 矢量裁切失败: {e}")
        return None

    def closeEvent(self, event):
        if self.task is not None:
            self.task.cancel()
        super().closeEvent(event)