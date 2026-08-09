# -*- coding: utf-8 -*-
"""
插件主类：负责注册菜单、工具栏按钮以及管理 DockWidget 声明周期
"""

import os
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .dockwidget import ImageInterpretDockWidget


class ImageInterpretPlugin:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dockwidget = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.action = QAction(icon, "遥感影像智能解译助手", self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&遥感影像智能解译助手", self.action)

    def toggle_dock(self):
        if self.dockwidget is None:
            self.dockwidget = ImageInterpretDockWidget(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dockwidget)
        else:
            self.dockwidget.setVisible(not self.dockwidget.isVisible())

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&遥感影像智能解译助手", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.dockwidget:
            self.dockwidget.cancel_running_task()
            self.iface.removeDockWidget(self.dockwidget)
            self.dockwidget.deleteLater()
            self.dockwidget = None