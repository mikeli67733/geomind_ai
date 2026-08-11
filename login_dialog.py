# -*- coding: utf-8 -*-
"""
用户名/密码登录、注册与修改密码对话框。

成功后通过 loggedIn 信号把 (username, token) 传给外部（dockwidget），
由外部负责持久化到 QSettings 并刷新账号状态面板。
"""

import re
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QTabWidget, QWidget, QMessageBox
)

from .auth_client import GeoMindAuthClient, AuthApiError
from .machine_id import get_machine_id  # 👈 导入机器码生成模块


class LoginDialog(QDialog):

    loggedIn = pyqtSignal(str, str)  # (username, access_token)

    def __init__(self, server_url_provider, token: str = "", username: str = "", parent=None):
        """
        server_url_provider: 一个无参可调用对象，返回当前填写的服务地址字符串。
        token: 已登录用户的 token（若传入则开启修改密码功能）
        username: 当前已登录用户名
        """
        super().__init__(parent)
        self.setWindowTitle("账号管理 - GeoMind AI")
        self.setMinimumWidth(340)
        self._server_url_provider = server_url_provider
        self._token = token
        self._username = username
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_login_tab(), "登录")
        self.tabs.addTab(self._build_register_tab(), "注册新账号")

        # 若已登录状态下打开弹窗，增加“修改密码”页签
        if self._token:
            self.tabs.addTab(self._build_change_pwd_tab(), "修改密码")

        layout.addWidget(self.tabs)
        self.setLayout(layout)

    def _build_login_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout()

        self.login_username_edit = QLineEdit()
        if self._username:
            self.login_username_edit.setText(self._username)

        self.login_password_edit = QLineEdit()
        self.login_password_edit.setEchoMode(QLineEdit.Password)
        form.addRow("用户名:", self.login_username_edit)
        form.addRow("密码:", self.login_password_edit)

        btn = QPushButton("登录")
        btn.setStyleSheet("font-weight: bold; padding: 6px;")
        btn.clicked.connect(self._do_login)
        self.login_password_edit.returnPressed.connect(self._do_login)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(btn)
        outer.addStretch()
        tab.setLayout(outer)
        return tab

    def _build_register_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout()

        self.reg_username_edit = QLineEdit()
        self.reg_username_edit.setPlaceholderText("3-32位字母/数字/下划线")
        self.reg_password_edit = QLineEdit()
        self.reg_password_edit.setEchoMode(QLineEdit.Password)
        self.reg_password_edit.setPlaceholderText("至少8位，须包含字母和数字")  # 👈 提示更新为 8 位
        self.reg_password_confirm_edit = QLineEdit()
        self.reg_password_confirm_edit.setEchoMode(QLineEdit.Password)

        form.addRow("用户名:", self.reg_username_edit)
        form.addRow("密码:", self.reg_password_edit)
        form.addRow("确认密码:", self.reg_password_confirm_edit)

        tip = QLabel("提示：注册即自动开通「免费版」，每天可免费解译 20 次。\n注意：单台设备仅允许注册 1 个免费账号。")
        tip.setStyleSheet("color: #888888; font-size: 11px;")
        tip.setWordWrap(True)

        btn = QPushButton("注册并登录")
        btn.setStyleSheet("font-weight: bold; padding: 6px;")
        btn.clicked.connect(self._do_register)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(tip)
        outer.addWidget(btn)
        outer.addStretch()
        tab.setLayout(outer)
        return tab

    def _build_change_pwd_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout()

        self.old_pwd_edit = QLineEdit()
        self.old_pwd_edit.setEchoMode(QLineEdit.Password)

        self.new_pwd_edit = QLineEdit()
        self.new_pwd_edit.setEchoMode(QLineEdit.Password)
        self.new_pwd_edit.setPlaceholderText("至少8位，须包含字母和数字")

        self.confirm_new_pwd_edit = QLineEdit()
        self.confirm_new_pwd_edit.setEchoMode(QLineEdit.Password)

        form.addRow("原密码:", self.old_pwd_edit)
        form.addRow("新密码:", self.new_pwd_edit)
        form.addRow("确认新密码:", self.confirm_new_pwd_edit)

        btn = QPushButton("确认修改密码")
        btn.setStyleSheet("font-weight: bold; padding: 6px;")
        btn.clicked.connect(self._do_change_password)

        outer = QVBoxLayout()
        outer.addLayout(form)
        outer.addWidget(btn)
        outer.addStretch()
        tab.setLayout(outer)
        return tab

    def _current_server_url(self) -> str:
        return (self._server_url_provider() or "").strip()

    def _validate_password_frontend(self, password: str, username: str = "") -> str:
        """前端预校验密码复杂度"""
        if len(password) < 8:
            return "密码长度不能少于 8 个字符"
        if not (re.search(r"[a-zA-Z]", password) and re.search(r"\d", password)):
            return "密码必须同时包含字母和数字"
        if username and password.lower() == username.lower():
            return "密码不能与用户名相同"
        return ""

    def _do_login(self):
        server_url = self._current_server_url()
        username = self.login_username_edit.text().strip()
        password = self.login_password_edit.text()

        if not server_url:
            QMessageBox.warning(self, "提示", "请先填写服务地址")
            return
        if not username or not password:
            QMessageBox.warning(self, "提示", "请输入用户名和密码")
            return

        client = GeoMindAuthClient(server_url)
        try:
            result = client.login(username, password)
        except AuthApiError as e:
            QMessageBox.critical(self, "登录失败", str(e))
            return

        self.loggedIn.emit(result["username"], result["access_token"])
        self.accept()

    def _do_register(self):
        server_url = self._current_server_url()
        username = self.reg_username_edit.text().strip()
        password = self.reg_password_edit.text()
        confirm = self.reg_password_confirm_edit.text()

        if not server_url:
            QMessageBox.warning(self, "提示", "请先填写服务地址")
            return
        if not username or not password:
            QMessageBox.warning(self, "提示", "请输入用户名和密码")
            return
        if password != confirm:
            QMessageBox.warning(self, "提示", "两次输入的密码不一致")
            return

        # 1. 前端先做一层弱密码校验
        err_msg = self._validate_password_frontend(password, username)
        if err_msg:
            QMessageBox.warning(self, "密码格式不符合要求", err_msg)
            return

        # 2. 获取本机设备 ID
        machine_id = get_machine_id()

        client = GeoMindAuthClient(server_url)
        try:
            # 3. 提交注册时附带 machine_id
            result = client.register(username, password, machine_id)
        except AuthApiError as e:
            QMessageBox.critical(self, "注册失败", str(e))
            return

        QMessageBox.information(self, "注册成功", "已自动登录，欢迎使用 GeoMind AI！")
        self.loggedIn.emit(result["username"], result["access_token"])
        self.accept()

    def _do_change_password(self):
        server_url = self._current_server_url()
        old_pwd = self.old_pwd_edit.text()
        new_pwd = self.new_pwd_edit.text()
        confirm_pwd = self.confirm_new_pwd_edit.text()

        if not old_pwd or not new_pwd:
            QMessageBox.warning(self, "提示", "请输入原密码和新密码")
            return
        if new_pwd != confirm_pwd:
            QMessageBox.warning(self, "提示", "两次输入的新密码不一致")
            return

        err_msg = self._validate_password_frontend(new_pwd)
        if err_msg:
            QMessageBox.warning(self, "新密码格式不符合要求", err_msg)
            return

        client = GeoMindAuthClient(server_url, token=self._token)
        try:
            res = client.change_password(old_pwd, new_pwd)
            QMessageBox.information(self, "修改成功", res.get("message", "密码修改成功！"))
            self.accept()
        except AuthApiError as e:
            QMessageBox.critical(self, "修改密码失败", str(e))