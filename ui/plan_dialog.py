# -*- coding: utf-8 -*-
"""
Plan management dialog — free plan info, card redemption, custom service.
"""
import os
import webbrowser

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTabWidget, QWidget, QMessageBox,
)

from ..api.auth_client import GeoMindAuthClient, AuthApiError
from ..core.constants import (
    FREE_PLAN_DAILY_QUOTA, PRO_PLAN_PRICE_YUAN, PRO_PLAN_DAYS,
    CUSTOM_PLAN_CONTACT_TEXT, XIANYU_PRODUCT_URL,
)
from ..core.compat import ALIGN_CENTER, KEEP_ASPECT_RATIO, SMOOTH_TRANSFORMATION
from ..core.config import settings

class PlanDialog(QDialog):
    """Plan management and card-key redemption dialog."""

    def __init__(self, server_url: str, token: str, account_info: dict,
                 plugin_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("套餐与卡密兑换 - GeoMind AI")
        self.setMinimumWidth(380)
        self.server_url = server_url
        self.token = token
        self.account_info = account_info or {}
        self.plugin_dir = plugin_dir
        self.client = GeoMindAuthClient(server_url, token)
        self.account_refreshed = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()
        tabs = QTabWidget()
        tabs.addTab(self._build_free_tab(), "1. 免费版")
        tabs.addTab(self._build_pro_tab(), "2. 卡密兑换/包月")
        tabs.addTab(self._build_custom_tab(), "3. 定制/私有化")
        layout.addWidget(tabs)
        self.setLayout(layout)

    def _build_free_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout()
        used = self.account_info.get("quota_used_today", 0)
        limit = self.account_info.get("quota_limit_today")
        limit = limit if limit is not None else FREE_PLAN_DAILY_QUOTA

        title = QLabel("免费版")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        v.addWidget(title)
        desc = QLabel(f"每天可免费解译 {FREE_PLAN_DAILY_QUOTA} 次，每日 0 点（服务器时间）自动重置。")
        desc.setWordWrap(True)
        v.addWidget(desc)
        status = QLabel(f"今日已用: {used} / {limit}")
        status.setStyleSheet("color: #333333; margin-top: 8px;")
        v.addWidget(status)
        v.addStretch()
        tab.setLayout(v)
        return tab

    def _build_pro_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout()

        title = QLabel("包月会员 (卡密兑换)")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        v.addWidget(title)

        plan = self.account_info.get("plan")
        if plan == "pro":
            expire = self.account_info.get("pro_expire_at") or "未知"
            cur_status = QLabel(f"当前状态: 会员生效中，到期时间 {expire}")
            cur_status.setStyleSheet("color: #2e7d32;")
        else:
            cur_status = QLabel("当前状态: 免费版")
            cur_status.setStyleSheet("color: #888888;")
        cur_status.setWordWrap(True)
        v.addWidget(cur_status)

        tip_box = QLabel(
            "如何获取卡密？\n点击下方按钮前往闲鱼下单购买，付款后自动发货卡密，"
            "收到后粘贴在下方即可秒级兑换！"
        )
        tip_box.setWordWrap(True)
        tip_box.setStyleSheet("color: #1565c0; background-color: #e3f2fd; padding: 8px; border-radius: 4px; margin-top: 6px;")
        v.addWidget(tip_box)

        self.buy_xianyu_btn = QPushButton("点击跳转闲鱼购买卡密 (自动发货)")
        self.buy_xianyu_btn.setStyleSheet(
            "font-weight: bold; font-size: 12px; background-color: #FFDA44; color: #222222; "
            "padding: 8px; border: 1px solid #E6C200; border-radius: 4px; margin-top: 4px; margin-bottom: 6px;"
        )
        self.buy_xianyu_btn.clicked.connect(self._open_xianyu_url)
        v.addWidget(self.buy_xianyu_btn)

        redeem_layout = QHBoxLayout()
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("在此粘贴您的兑换码 (如 GEOMIND-PRO-xxx)")
        redeem_layout.addWidget(self.code_edit)
        self.redeem_btn = QPushButton("立即兑换")
        self.redeem_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.redeem_btn.clicked.connect(self._do_redeem)
        redeem_layout.addWidget(self.redeem_btn)
        v.addLayout(redeem_layout)

        self.msg_label = QLabel("")
        self.msg_label.setWordWrap(True)
        v.addWidget(self.msg_label)
        v.addStretch()
        tab.setLayout(v)
        return tab

    def _build_custom_tab(self) -> QWidget:
        tab = QWidget()
        v = QVBoxLayout()
        title = QLabel("定制服务 / 私有化部署")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        v.addWidget(title)
        desc = QLabel(
            CUSTOM_PLAN_CONTACT_TEXT
            + "（按解译区域面积计费，具体价格请联系作者报价）"
        )
        desc.setWordWrap(True)
        v.addWidget(desc)

        qr_path = os.path.join(self.plugin_dir, "qr_code.png")
        qr_label = QLabel()
        qr_label.setAlignment(ALIGN_CENTER)
        if os.path.exists(qr_path):
            pixmap = QPixmap(qr_path).scaled(150, 150, KEEP_ASPECT_RATIO, SMOOTH_TRANSFORMATION)
            qr_label.setPixmap(pixmap)
        else:
            qr_label.setText("[联系作者二维码]")
            qr_label.setStyleSheet("color: #888888; border: 1px dashed #CCCCCC; padding: 10px;")
        v.addWidget(qr_label)
        v.addStretch()
        tab.setLayout(v)
        return tab

    def _open_xianyu_url(self):
        url = settings.xianyu_url()  # <-- 动态获取 URL
        if url:
            webbrowser.open(url)
        else:
            QMessageBox.information(self, "提示", "暂未配置闲鱼购买链接，请联系管理员")

    def _do_redeem(self):
        code = self.code_edit.text().strip()
        if not code:
            QMessageBox.warning(self, "提示", "请输入卡密兑换码")
            return
        self.redeem_btn.setEnabled(False)
        self.msg_label.setText("正在校验兑换卡密...")
        try:
            res = self.client.redeem_card(code)
            self.msg_label.setText(f"兑换成功: {res['message']}")
            self.msg_label.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self.account_refreshed = True
            self.code_edit.clear()
            QMessageBox.information(self, "兑换成功", res["message"])
        except AuthApiError as e:
            self.msg_label.setText(f"兑换失败: {e}")
            self.msg_label.setStyleSheet("color: #d32f2f;")
            QMessageBox.critical(self, "兑换失败", str(e))
        finally:
            self.redeem_btn.setEnabled(True)
