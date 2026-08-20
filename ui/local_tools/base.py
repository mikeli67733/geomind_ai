# -*- coding: utf-8 -*-
"""
Base widget for local (offline) GIS/RS processing tools.

Every tool page shares the same scaffold: a reference to the main dock
and canvas, an optional extent-selection tool, and a ``_build_ui`` hook.
"""
from qgis.PyQt.QtWidgets import QWidget


class BaseLocalToolWidget(QWidget):
    """Common scaffold for local (offline) processing tools."""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self.canvas = main_dock.canvas
        self.extent_tool = None
        self.selected_extent = None
        self._build_ui()

    def _build_ui(self):
        """Override in subclasses to construct the UI."""
        raise NotImplementedError
