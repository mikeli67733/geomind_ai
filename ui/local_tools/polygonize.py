# -*- coding: utf-8 -*-
"""Raster to vector polygonization with sieve filtering widget."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QPushButton, QSpinBox,
    QMessageBox, QGroupBox, QApplication,
)
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import raster_polygonize
from .base import BaseLocalToolWidget


class RasterPolygonizeWidget(BaseLocalToolWidget):
    """Raster to vector polygonization with sieve filtering widget."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入单波段/分类栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setObjectName("layer_combo")
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 过滤与转换参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("过滤孤立碎斑阈值 (像元数):"), 0, 0)
        self.spin_sieve = QSpinBox(); self.spin_sieve.setObjectName("spin_sieve")
        self.spin_sieve.setRange(0, 500); self.spin_sieve.setValue(4)
        param_grid.addWidget(self.spin_sieve, 0, 1)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行矢量化")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 一键提取矢量图斑 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_polygonize)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🧩 将栅格掩膜转为矢量 Polygon 面图层并滤除孤立碎点")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_polygonize(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择栅格图层")
            return
        self.mark_run_started()
        self.status_label.setText("正在提取矢量多边形...")
        QApplication.processEvents()
        try:
            out_path = raster_polygonize(layer.source(), self.spin_sieve.value())
            self.status_label.setText("矢量化成功！已添加到图层列表")
            self.record_local_run(
                "ok", summary=f"栅格矢量化 (碎斑过滤 {self.spin_sieve.value()})",
                output_paths=[out_path])
            QMessageBox.information(self, "成功", "栅格已转为矢量 Polygon 图斑！")
        except Exception as e:
            self.status_label.setText("矢量化失败")
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"转换失败: {e}")
