# -*- coding: utf-8 -*-
"""
Account settings page — user info, quota display, gateway refresh.
"""
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QMessageBox, QApplication,
)

from ..core.constants import PLAN_LABELS, FREE_PLAN_DAILY_QUOTA


class AccountSettingsPage(QWidget):
    """Account settings and server gateway management page."""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # 1. User info card
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

        # 2. Network & server status
        server_group = QGroupBox("服务网关")
        server_layout = QVBoxLayout(server_group)

        notice_banner = QLabel("🌸 提示：若遇到连接超时或网络异常，可点击下方按钮同步最新服务节点。")
        notice_banner.setObjectName("noticeBanner")
        notice_banner.setWordWrap(True)
        server_layout.addWidget(notice_banner)

        self.refresh_url_btn = QPushButton("🔄 同步并刷新最新网关")
        self.refresh_url_btn.clicked.connect(self._refresh_url)
        server_layout.addWidget(self.refresh_url_btn)

        layout.addWidget(server_group)
        layout.addStretch()

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

    def _refresh_url(self):
        self.refresh_url_btn.setEnabled(False)
        self.refresh_url_btn.setText("正在同步网关...")
        QApplication.processEvents()
        try:
            new_url = self.dock.get_remote_or_default_url(force_refresh=True)
            if new_url and new_url != self.dock.FALLBACK_SERVER_URL:
                QMessageBox.information(self, "成功", "已成功同步最新服务网关通道！")
            else:
                QMessageBox.warning(self, "提示", "未能拉取到最新网关，已自动启用默认通道。")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"刷新网关失败: {e}")
        finally:
            self.refresh_url_btn.setEnabled(True)
            self.refresh_url_btn.setText("🔄 同步并刷新最新网关")
