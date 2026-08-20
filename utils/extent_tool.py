# -*- coding: utf-8 -*-
"""
Map tool for drag-rectangle extent selection on the QGIS canvas.

Compatible with PyQt5 and PyQt6 (QGIS 3.16 – 3.42+).
"""
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsRectangle, QgsWkbTypes, QgsPointXY
from qgis.gui import QgsMapTool, QgsRubberBand

from ..core.compat import LEFT_BUTTON, RIGHT_BUTTON, KEY_ESCAPE, POLYGON_GEOMETRY


class ExtentSelectTool(QgsMapTool):
    """Drag a rectangle on the map canvas to select an extent."""

    extentSelected = pyqtSignal(QgsRectangle)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rubber_band = QgsRubberBand(canvas, POLYGON_GEOMETRY)
        self.rubber_band.setColor(QColor(255, 0, 0, 60))
        self.rubber_band.setStrokeColor(QColor(255, 0, 0, 200))
        self.rubber_band.setWidth(2)
        self.start_point = None
        self.is_dragging = False

    def canvasPressEvent(self, event):
        if event.button() == LEFT_BUTTON:
            self.start_point = event.mapPoint()
            self.is_dragging = True
            self.rubber_band.reset(POLYGON_GEOMETRY)
        elif event.button() == RIGHT_BUTTON:
            self.cancel()

    def canvasMoveEvent(self, event):
        if not self.is_dragging or self.start_point is None:
            return
        current_point = event.mapPoint()
        rect = QgsRectangle(self.start_point, current_point)
        self._show_rect(rect)

    def canvasReleaseEvent(self, event):
        if event.button() != LEFT_BUTTON or not self.is_dragging:
            return
        self.is_dragging = False
        end_point = event.mapPoint()
        rect = QgsRectangle(self.start_point, end_point)
        rect.normalize()
        if rect.width() == 0 or rect.height() == 0:
            self.cancel()
            return
        self.extentSelected.emit(rect)

    def keyPressEvent(self, event):
        if event.key() == KEY_ESCAPE:
            self.cancel()

    def cancel(self):
        self.is_dragging = False
        self.start_point = None
        self.rubber_band.reset(POLYGON_GEOMETRY)

    def deactivate(self):
        self.rubber_band.reset(POLYGON_GEOMETRY)
        super().deactivate()

    def _show_rect(self, rect):
        pts = [
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
        ]
        self.rubber_band.reset(POLYGON_GEOMETRY)
        for p in pts:
            self.rubber_band.addPoint(p, False)
        self.rubber_band.closePoints()
        self.rubber_band.show()
