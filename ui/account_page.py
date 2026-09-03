# -*- coding: utf-8 -*-
"""
Account settings page — user info, quota display, gateway refresh.
"""
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QMessageBox, QApplication, QRadioButton, QLineEdit, QButtonGroup
)

from ..core.constants import PLAN_LABELS, FREE_PLAN_DAILY_QUOTA
from ..core.config import settings


class AccountSettingsPage(QWidget):
    """Account settings and server gateway management page."""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()
        self._load_gateway_settings()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # ----------------------------------------------------
        # 1. User info card
        # ----------------------------------------------------
        acc_group = QGroupBox("用户信息与凭证")
        acc_layout = QVBoxLayout(acc_group)

        self.account_status_label = QLabel("尚未登录")
        self.account_status_label.setStyleSheet("color: #64748b; font-size: 13px;")
        acc_layout.addWidget(self.account_status_label)

        self.quota_label = QLabel("")
        acc_layout.addWidget(self.quota_label)

        btn_row = QHBoxLayout()
        self.login_btn = QPushButton("🔑 登录 / 注册")
        self.login_btn.setObjectName("runBtn")
        self.login_btn.clicked.connect(self.dock.open_login_dialog)
        btn_row.addWidget(self.login_btn)

        self.logout_btn = QPushButton("退出登录")
        self.logout_btn.setObjectName("cancelBtn")
        self.logout_btn.clicked.connect(self.dock.logout)
        self.logout_btn.setVisible(False)
        btn_row.addWidget(self.logout_btn)

        self.upgrade_btn = QPushButton("💎 套餐与充值")
        self.upgrade_btn.clicked.connect(self.dock.open_plan_dialog)
        btn_row.addWidget(self.upgrade_btn)
        acc_layout.addLayout(btn_row)
        layout.addWidget(acc_group)

        # ----------------------------------------------------
        # 2. Network & server gateway status
        # ----------------------------------------------------
        server_group = QGroupBox("服务网关配置")
        server_layout = QVBoxLayout(server_group)
        server_layout.setSpacing(10)

        # 当前生效状态展示（隐藏云端真实URL，本地真实展示）
        self.current_url_label = QLabel("当前生效网关: 加载中...")
        self.current_url_label.setStyleSheet("color: #475569; font-size: 12px;")
        self.current_url_label.setWordWrap(True)
        server_layout.addWidget(self.current_url_label)

        # 模式切换单选按钮组
        self.mode_button_group = QButtonGroup(self)

        # --- 方式一：官方云端自动同步 ---
        self.radio_cloud = QRadioButton("方式一：官方云端网关 (自动解析最新通道)")
        self.mode_button_group.addButton(self.radio_cloud, 1)
        server_layout.addWidget(self.radio_cloud)

        cloud_container = QWidget()
        cloud_layout = QVBoxLayout(cloud_container)
        cloud_layout.setContentsMargins(20, 0, 0, 0)
        cloud_layout.setSpacing(6)

        cloud_notice = QLabel("提示：若遇网络波动或请求超时，可点击下方按钮同步最新服务节点。")
        cloud_notice.setStyleSheet("color: #94a3b8; font-size: 11px;")
        cloud_notice.setWordWrap(True)
        cloud_layout.addWidget(cloud_notice)

        self.refresh_url_btn = QPushButton("🔄 同步并更新最新网关")
        self.refresh_url_btn.clicked.connect(self._refresh_cloud_url)
        cloud_layout.addWidget(self.refresh_url_btn)
        server_layout.addWidget(cloud_container)
        self.cloud_container = cloud_container

        # --- 方式二：手动配置/本地私有化服务 ---
        self.radio_custom = QRadioButton("方式二：手动配置本地 / 私有专网服务")
        self.mode_button_group.addButton(self.radio_custom, 2)
        server_layout.addWidget(self.radio_custom)

        custom_container = QWidget()
        custom_layout = QVBoxLayout(custom_container)
        custom_layout.setContentsMargins(20, 0, 0, 0)
        custom_layout.setSpacing(6)

        custom_input_row = QHBoxLayout()
        self.custom_url_edit = QLineEdit()
        self.custom_url_edit.setPlaceholderText("例如: http://127.0.0.1:8000")
        custom_input_row.addWidget(self.custom_url_edit)

        self.save_custom_btn = QPushButton("保存并生效")
        self.save_custom_btn.clicked.connect(self._apply_custom_url)
        custom_input_row.addWidget(self.save_custom_btn)
        custom_layout.addLayout(custom_input_row)

        server_layout.addWidget(custom_container)
        self.custom_container = custom_container

        # 模式切换事件监听
        self.radio_cloud.toggled.connect(self._on_mode_toggled)

        layout.addWidget(server_group)
        layout.addStretch()

    # ----------------------------------------------------
    # 网关配置逻辑处理
    # ----------------------------------------------------
    def _load_gateway_settings(self):
        """从底层的 Settings 单例恢复网关配置"""
        mode = settings.gateway_mode()
        custom_url = settings.custom_server_url() or "http://127.0.0.1:8000"

        # 临时断开信号连接，避免加载时反复触发保存逻辑
        self.radio_cloud.blockSignals(True)
        self.radio_custom.blockSignals(True)

        self.custom_url_edit.setText(custom_url)

        if mode == "custom":
            self.radio_custom.setChecked(True)
            self._apply_active_gateway(custom_url, is_custom=True)
        else:
            self.radio_cloud.setChecked(True)
            active_url = settings.server_url(force_refresh=False)
            self._apply_active_gateway(active_url, is_custom=False)

        self.radio_cloud.blockSignals(False)
        self.radio_custom.blockSignals(False)
        self._update_container_enabled_state()

    def _on_mode_toggled(self):
        """单选切换响应事件"""
        self._update_container_enabled_state()

        if self.radio_cloud.isChecked():
            settings.set_gateway(mode="cloud")
            active_url = settings.server_url(force_refresh=False)
            self._apply_active_gateway(active_url, is_custom=False)
        else:
            url = self.custom_url_edit.text().strip() or "http://127.0.0.1:8000"
            settings.set_gateway(mode="custom", custom_url=url)
            self._apply_active_gateway(url, is_custom=True)

    def _update_container_enabled_state(self):
        """根据当前单选项置灰或点亮输入区域"""
        is_cloud = self.radio_cloud.isChecked()
        self.cloud_container.setEnabled(is_cloud)
        self.custom_container.setEnabled(not is_cloud)

    def _refresh_cloud_url(self):
        """方式一：同步并拉取云端动态网关"""
        self.refresh_url_btn.setEnabled(False)
        self.refresh_url_btn.setText("正在同步网关...")
        QApplication.processEvents()
        try:
            # 强制从云端动态节点刷新
            new_url = settings.server_url(force_refresh=True)
            self._apply_active_gateway(new_url, is_custom=False)
            QMessageBox.information(self, "成功", "已成功同步并接入最新的官方服务通道！")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"同步网关失败: {e}")
        finally:
            self.refresh_url_btn.setEnabled(True)
            self.refresh_url_btn.setText("🔄 同步并更新最新网关")

    def _apply_custom_url(self):
        """方式二：应用并生效本地/私有网关"""
        url = self.custom_url_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入有效的服务器地址！")
            return

        # 补全协议头
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "http://" + url
            self.custom_url_edit.setText(url)

        url = url.rstrip("/")

        # 写入底层 Settings 统一配置并切换模式
        settings.set_gateway(mode="custom", custom_url=url)

        # 确保单选框切换
        if not self.radio_custom.isChecked():
            self.radio_custom.setChecked(True)
        else:
            self._apply_active_gateway(url, is_custom=True)

        QMessageBox.information(self, "已保存", f"已成功切换至私有网关：\n{url}")

    def _apply_active_gateway(self, url, is_custom=False):
        """将选定 URL 赋给 main_dock，并更新 UI"""
        if hasattr(self.dock, "set_server_url"):
            self.dock.set_server_url(url)
        else:
            self.dock.server_url = url

        self._update_current_url_display(url, is_custom)

    def _update_current_url_display(self, url, is_custom=False):
        """更新状态栏展示（云端模式脱敏隐藏真实URL）"""
        if is_custom:
            self.current_url_label.setText(
                f"当前生效网关: <b style='color: #475569;'>[本地/私有]</b> "
                f"<span style='color: #2563eb;'>{url}</span>"
            )
        else:
            status_text = "🟢 官方智能高速通道 (已连接)" if url else "⚪ 默认通道"
            self.current_url_label.setText(
                f"当前生效网关: <b style='color: #475569;'>[官方云端]</b> "
                f"<span style='color: #16a34a; font-weight: bold;'>{status_text}</span>"
            )

    # ----------------------------------------------------
    # 用户登录态更新
    # ----------------------------------------------------
    def update_account_ui(self, token, username, account_info):
        """Refresh the account display based on login state."""
        if not token:
            self.account_status_label.setText(
                "尚未登录，AI 解译与 Copilot 助手需登录后使用 (本地遥感工具免费使用)"
            )
            self.account_status_label.setStyleSheet("color: #64748b;")
            self.quota_label.setText("")
            self.login_btn.setVisible(True)
            self.logout_btn.setVisible(False)
            return

        self.login_btn.setVisible(False)
        self.logout_btn.setVisible(True)
        plan = account_info.get("plan", "free")
        plan_label = PLAN_LABELS.get(plan, plan)
        self.account_status_label.setText(
            f"已登录: <b>{username}</b><br>当前套餐: <b>{plan_label}</b>"
        )
        self.account_status_label.setStyleSheet("color: #15803d; font-size: 13px;")

        if plan == "free":
            used = account_info.get("quota_used_today", 0)
            limit = account_info.get("quota_limit_today") or FREE_PLAN_DAILY_QUOTA
            self.quota_label.setText(
                f"今日剩余 AI 额度: <b>{max(limit - used, 0)}</b> / {limit} 次"
            )
        elif plan == "pro":
            expire = account_info.get("pro_expire_at", "未知")
            self.quota_label.setText(f"包月会员生效中 (到期: {expire})")
        else:
            self.quota_label.setText("定制版本，无限制")