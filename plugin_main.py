# -*- coding: utf-8 -*-
"""
QGIS plugin entry point.

Registers the toolbar button and plugin menu, and manages the
lifecycle of the main GeoMind AI dock widget.
"""
import os

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .core.constants import (
    SETTINGS_ORG, SETTINGS_APP,
    SETTINGS_KEY_DOCK_VISIBLE, SETTINGS_KEY_DOCK_AREA,
)
from .core.logger import get_logger
from .ui.dock_widget import ImageInterpretDockWidget

logger = get_logger(__name__)

PLUGIN_NAME = "遥感影像智能解译助手"
MENU_ENTRY = f"&{PLUGIN_NAME}"


class ImageInterpretPlugin:
    """QGIS plugin entry class: menu/toolbar registration and dock lifecycle."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.action = None
        self.dockwidget = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(icon, PLUGIN_NAME, self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(MENU_ENTRY, self.action)
        logger.info("GeoMind AI plugin initialized")

    def toggle_dock(self):
        if self.dockwidget is None:
            self.dockwidget = ImageInterpretDockWidget(self.iface, self.iface.mainWindow())
            area = self._restore_dock_area()
            self.iface.addDockWidget(area, self.dockwidget)
            # Respect the persisted visibility: if the dock was closed before
            # shutdown, keep it hidden until the user opens it again.
            self.dockwidget.setVisible(self._restore_dock_visibility())
        else:
            self.dockwidget.setVisible(not self.dockwidget.isVisible())

    def _restore_dock_area(self) -> Qt.DockWidgetArea:
        """Return the persisted dock area, defaulting to the right side."""
        s = QSettings(SETTINGS_ORG, SETTINGS_APP)
        area_val = s.value(SETTINGS_KEY_DOCK_AREA, int(Qt.RightDockWidgetArea))
        try:
            area = Qt.DockWidgetArea(int(area_val))
            if area not in (
                Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea,
                Qt.TopDockWidgetArea, Qt.BottomDockWidgetArea,
            ):
                return Qt.RightDockWidgetArea
            return area
        except Exception:
            return Qt.RightDockWidgetArea

    def _restore_dock_visibility(self) -> bool:
        """Return whether the dock should be visible after startup."""
        s = QSettings(SETTINGS_ORG, SETTINGS_APP)
        visible = s.value(SETTINGS_KEY_DOCK_VISIBLE, "1")
        return str(visible).strip() != "0"

    def unload(self):
        if self.dockwidget:
            # Persist final state (visibility, area, active page) before teardown
            s = QSettings(SETTINGS_ORG, SETTINGS_APP)
            s.setValue(SETTINGS_KEY_DOCK_VISIBLE, "1" if self.dockwidget.isVisible() else "0")
            s.setValue(SETTINGS_KEY_DOCK_AREA, self.dockwidget.dock_area())
            self.dockwidget.save_dock_state(visible=self.dockwidget.isVisible())
            self.dockwidget.cancel_running_task()
            self.iface.removeDockWidget(self.dockwidget)
            self.dockwidget.deleteLater()
            self.dockwidget = None
        if self.action:
            self.iface.removePluginMenu(MENU_ENTRY, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        logger.info("GeoMind AI plugin unloaded")