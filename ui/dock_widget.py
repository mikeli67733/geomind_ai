# -*- coding: utf-8 -*-
"""
GeoMind AI - Main Dock Widget container.

Assembles the Copilot chat page, the account center and the 18 specialized
tool pages into a single QStackedWidget, and provides the shared navigation,
server URL resolution, login state and running-task management that every
child widget relies on.
"""
import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QToolButton, QStackedWidget, QScrollArea, QFrame, QMessageBox,
)

from ..core.config import settings
from ..core.constants import (
    FALLBACK_SERVER_URL,
    SETTINGS_ORG, SETTINGS_APP,
    SETTINGS_KEY_TOKEN, SETTINGS_KEY_USERNAME,
    SETTINGS_KEY_DOCK_VISIBLE, SETTINGS_KEY_DOCK_PAGE, SETTINGS_KEY_DOCK_AREA,
    find_class_ids_by_keywords,
)
from ..core.exceptions import AuthApiError
from ..core.logger import get_logger
from ..api.auth_client import GeoMindAuthClient
from ..utils.machine_id import get_machine_id

from .theme import MAIN_DOCK_QSS, TOP_BAR_QSS
from .copilot_widget import LlmCopilotWidget
from .account_page import AccountSettingsPage
from .login_dialog import LoginDialog
from .plan_dialog import PlanDialog
from .base_task_widget import (
    LanduseMultiTaskWidget,
    SingleThemeExtractionWidget,
    Sam3TaskWidget,
    ChangeDetectionTaskWidget,
)
from .local_tools import LOCAL_TOOL_PAGES

logger = get_logger(__name__)


class ImageInterpretDockWidget(QDockWidget):
    """Main dock container: Copilot chat + account center + 18 tool pages."""

    # Exposed so pages (e.g. AccountSettingsPage) can detect whether URL
    # resolution ultimately fell back to the built-in default.
    FALLBACK_SERVER_URL = FALLBACK_SERVER_URL

    def __init__(self, iface, parent=None):
        super().__init__("GeoAI 遥感智能解译", parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.machine_id = get_machine_id()

        self.token = ""
        self.username = ""
        self.account_info = {}
        self.active_running_task = None

        self._build_ui()
        self._load_settings()
        self._restore_dock_state()
        self._try_restore_login()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        container = QWidget()
        container.setObjectName("dockContainer")
        container.setStyleSheet(MAIN_DOCK_QSS)

        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # -- Top brand navigation bar -----------------------------------
        top_bar_frame = QFrame()
        top_bar_frame.setObjectName("topBar")
        top_bar_frame.setStyleSheet(TOP_BAR_QSS)
        top_bar = QHBoxLayout(top_bar_frame)
        top_bar.setContentsMargins(10, 6, 12, 6)
        top_bar.setSpacing(8)

        self.back_btn = QToolButton()
        self.back_btn.setObjectName("iconBtn")
        self.back_btn.setText("💬 返回 AI 对话")
        self.back_btn.setToolTip("返回 Copilot 对话主窗口")
        self.back_btn.clicked.connect(self.show_copilot_page)
        self.back_btn.setVisible(False)
        top_bar.addWidget(self.back_btn)

        self.brand_logo = QLabel("🛰️")
        self.brand_logo.setObjectName("brandLogo")
        self.brand_logo.setFixedSize(32, 32)
        self.brand_logo.setAlignment(Qt.AlignCenter)
        top_bar.addWidget(self.brand_logo)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        self.title_label = QLabel("GeoMind AI Copilot")
        self.title_label.setObjectName("brandTitle")
        title_box.addWidget(self.title_label)
        self.subtitle_label = QLabel("遥感智能解译 · AI 对话")
        self.subtitle_label.setObjectName("brandSubtitle")
        title_box.addWidget(self.subtitle_label)
        top_bar.addLayout(title_box)

        top_bar.addStretch()

        self.account_btn = QToolButton()
        self.account_btn.setObjectName("iconBtn")
        self.account_btn.setText("🧸 设置")
        self.account_btn.setToolTip("账号与网络设置")
        self.account_btn.clicked.connect(self.show_account_page)
        top_bar.addWidget(self.account_btn)
        main_layout.addWidget(top_bar_frame)

        # -- Multi-page stack ------------------------------------------
        self.stack = QStackedWidget()

        # Index 0: Copilot chat home page
        self.copilot_page = LlmCopilotWidget(self)
        self.stack.addWidget(self.copilot_page)

        # Index 1: Account center
        self.account_page = AccountSettingsPage(self)
        self.stack.addWidget(self.account_page)

        # Free local tools (indexes 2-11)
        self.task_pages = {}
        for page_key, title, factory in LOCAL_TOOL_PAGES:
            self._register_page(page_key, title, factory(self))

        # Dynamic feature-class id resolution for AI theme extraction
        building_ids = find_class_ids_by_keywords(
            ["建筑", "住宅", "工业", "房屋", "厂房"], fallback_id="5,6,7")
        road_ids = find_class_ids_by_keywords(
            ["路", "道", "交通"], fallback_id="4")
        water_ids = find_class_ids_by_keywords(
            ["水", "河", "湖"], fallback_id="8")
        veg_ids = find_class_ids_by_keywords(
            ["林", "草", "树"], fallback_id="2,3")
        farm_ids = find_class_ids_by_keywords(
            ["耕", "农", "田"], fallback_id="1")

        # AI deep-learning interpretation models (indexes 12-19)
        self._register_page("task_landuse_multi", "🌻 土地利用全要素解译",
                            LanduseMultiTaskWidget(self))
        self._register_page("task_building", "🏡 建筑物专项提取",
                            SingleThemeExtractionWidget(
                                self, building_ids,
                                "自动识别选定区域内的城镇建筑、农村住宅与厂房轮廓。"))
        self._register_page("task_road", "🚗 道路交通专项提取",
                            SingleThemeExtractionWidget(
                                self, road_ids,
                                "自动提取公路、街道、主干道及乡村道路等交通网络。"))
        self._register_page("task_water", "🐬 水系水体专项提取",
                            SingleThemeExtractionWidget(
                                self, water_ids,
                                "自动识别河流、湖泊、坑塘、水库等地表水体边界。"))
        self._register_page("task_vegetation", "🍄 林草植被专项提取",
                            SingleThemeExtractionWidget(
                                self, veg_ids,
                                "精准识别林地、灌木林与天然草地。"))
        self._register_page("task_farmland", "🥕 农田耕地专项提取",
                            SingleThemeExtractionWidget(
                                self, farm_ids,
                                "提取农业种植用地、水浇地与旱地范围。"))
        self._register_page("task_sam3", "🌟 SAM3 交互提示解译",
                            Sam3TaskWidget(self))
        self._register_page("task_change", "🐥 深度双期变化检测",
                            ChangeDetectionTaskWidget(self))

        # Wrap stack in a scroll area so narrow docks remain usable
        scroll_area = QScrollArea()
        scroll_area.setWidget(self.stack)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        main_layout.addWidget(scroll_area)

        self.setWidget(container)

    def _register_page(self, key: str, title: str, widget: QWidget) -> None:
        """Add a tool page to the stack and record its navigation entry."""
        self.task_pages[key] = (title, self.stack.addWidget(widget))

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def show_copilot_page(self):
        """Return to the Copilot chat home page."""
        self.stack.setCurrentIndex(0)
        self.title_label.setText("GeoMind AI Copilot")
        self.subtitle_label.setText("遥感智能解译 · AI 对话")
        self.back_btn.setVisible(False)
        self.account_btn.setVisible(True)

    def show_account_page(self):
        """Enter the account & settings page."""
        self.stack.setCurrentIndex(1)
        self.title_label.setText("个人中心与设置")
        self.subtitle_label.setText("账号 · 网络 · 套餐")
        self.back_btn.setVisible(True)
        self.account_btn.setVisible(False)

    def navigate_to_task(self, task_key: str):
        """Jump directly to a tool page (used by the Tools menu)."""
        entry = self.task_pages.get(task_key)
        if not entry:
            logger.warning("Unknown task key: %s", task_key)
            return
        title, page_idx = entry
        self.stack.setCurrentIndex(page_idx)
        self.title_label.setText(title)
        self.subtitle_label.setText("专项工具 · 智能解译")
        self.back_btn.setVisible(True)
        self.account_btn.setVisible(True)

    # ------------------------------------------------------------------
    # Server URL management
    # ------------------------------------------------------------------
    def get_remote_or_default_url(self, force_refresh: bool = False) -> str:
        """Return the effective backend URL via the central settings facade."""
        return settings.server_url(force_refresh=force_refresh)

    def current_server_url(self) -> str:
        return self.get_remote_or_default_url()

    # ------------------------------------------------------------------
    # Settings & login state
    # ------------------------------------------------------------------
    def _load_settings(self):
        self.get_remote_or_default_url(force_refresh=False)

    # ------------------------------------------------------------------
    # Dock state persistence
    # ------------------------------------------------------------------
    def _restore_dock_state(self):
        """Restore the last visited tool page after a QGIS restart."""
        page_key = self.settings.value(SETTINGS_KEY_DOCK_PAGE, "")
        if page_key and page_key in self.task_pages:
            self.navigate_to_task(str(page_key))

    def save_dock_state(self, visible: bool):
        """Persist current dock visibility and active page."""
        self.settings.setValue(SETTINGS_KEY_DOCK_VISIBLE, "1" if visible else "0")
        for page_key, (_title, page_idx) in self.task_pages.items():
            if page_idx == self.stack.currentIndex():
                self.settings.setValue(SETTINGS_KEY_DOCK_PAGE, page_key)
                break

    def dock_area(self) -> int:
        """Return the current dock area as an int (Qt.DockWidgetArea)."""
        try:
            main_window = self.iface.mainWindow()
            return int(main_window.dockWidgetArea(self))
        except Exception:
            return int(Qt.RightDockWidgetArea)

    def _try_restore_login(self):
        token = self.settings.value(SETTINGS_KEY_TOKEN, "")
        username = self.settings.value(SETTINGS_KEY_USERNAME, "")
        if not token or not username:
            self.account_page.update_account_ui("", "", {})
            return
        self.token = str(token).strip()
        self.username = str(username).strip()
        self.refresh_account_info(silent=True)

    def open_login_dialog(self):
        dialog = LoginDialog(self.current_server_url, self)
        dialog.loggedIn.connect(self._on_logged_in)
        dialog.exec_()

    def _on_logged_in(self, username: str, token: str):
        self.username = username
        self.token = token
        self.settings.setValue(SETTINGS_KEY_TOKEN, token)
        self.settings.setValue(SETTINGS_KEY_USERNAME, username)
        self.refresh_account_info()

    def logout(self):
        self.token = ""
        self.username = ""
        self.account_info = {}
        self.settings.remove(SETTINGS_KEY_TOKEN)
        self.settings.remove(SETTINGS_KEY_USERNAME)
        self.account_page.update_account_ui("", "", {})

    def refresh_account_info(self, silent: bool = False):
        if not self.token:
            self.account_page.update_account_ui("", "", {})
            return
        client = GeoMindAuthClient(self.current_server_url(), self.token)
        try:
            self.account_info = client.get_me()
        except AuthApiError as exc:
            self.logout()
            if not silent:
                QMessageBox.warning(
                    self, "提示",
                    f"获取账号信息失败: {exc}\n\n提示：可在设置页刷新网关。")
            return
        self.account_page.update_account_ui(self.token, self.username, self.account_info)

    def open_plan_dialog(self):
        if not self.token:
            QMessageBox.information(self, "提示", "请先登录后再查看套餐")
            self.open_login_dialog()
            return
        dialog = PlanDialog(
            self.current_server_url(), self.token, self.account_info,
            self.plugin_dir, self)
        dialog.exec_()
        if dialog.account_refreshed:
            self.refresh_account_info(silent=True)

    # ------------------------------------------------------------------
    # Running task management
    # ------------------------------------------------------------------
    def cancel_running_task(self):
        if self.active_running_task is not None:
            try:
                self.active_running_task.cancel()
            except Exception as exc:
                logger.debug("Failed to cancel running task: %s", exc)

    def closeEvent(self, event):
        self.save_dock_state(visible=False)
        self.cancel_running_task()
        super().closeEvent(event)