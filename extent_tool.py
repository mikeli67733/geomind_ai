# -*- coding: utf-8 -*-
"""
自定义地图工具：鼠标拖拽框选矩形范围
兼容 PyQt5 与 PyQt6 (QGIS 3.16 ~ 3.42+)
"""

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsRectangle, QgsWkbTypes, QgsPointXY
from qgis.gui import QgsMapTool, QgsRubberBand

# PyQt5 / PyQt6 枚举兼容
LEFT_BUTTON = getattr(Qt, 'LeftButton', None)
if LEFT_BUTTON is None:
    LEFT_BUTTON = Qt.MouseButton.LeftButton

RIGHT_BUTTON = getattr(Qt, 'RightButton', None)
if RIGHT_BUTTON is None:
    RIGHT_BUTTON = Qt.MouseButton.RightButton

KEY_ESCAPE = getattr(Qt, 'Key_Escape', None)
if KEY_ESCAPE is None:
    KEY_ESCAPE = Qt.Key.Key_Escape


class ExtentSelectTool(QgsMapTool):

    extentSelected = pyqtSignal(QgsRectangle)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.rubber_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(QColor(255, 0, 0, 60))
        self.rubber_band.setStrokeColor(QColor(255, 0, 0, 200))
        self.rubber_band.setWidth(2)
        self.start_point = None
        self.is_dragging = False

    def canvasPressEvent(self, event):
        if event.button() == LEFT_BUTTON:
            self.start_point = event.mapPoint()
            self.is_dragging = True
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
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
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def deactivate(self):
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        super().deactivate()

    def _show_rect(self, rect):
        pts = [
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum())
        ]
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        for p in pts:
            self.rubber_band.addPoint(p, False)
        self.rubber_band.closePoints()
        self.rubber_band.show()