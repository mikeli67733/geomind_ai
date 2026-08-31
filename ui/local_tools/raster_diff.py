# -*- coding: utf-8 -*-
"""Dual-period pixel-level difference detection widget."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QPushButton, QSpinBox,
    QDoubleSpinBox, QCheckBox, QMessageBox, QGroupBox, QApplication,
)
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import raster_diff
from .base import BaseLocalToolWidget


class RasterDiffChangeWidget(BaseLocalToolWidget):
    """Dual-period pixel-level difference detection widget."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入双期影像")
        layer_v = QVBoxLayout(layer_group)
        layer_v.addWidget(QLabel("基准期 (T1 前期):"))
        self.combo_t1 = QgsMapLayerComboBox(); self.combo_t1.setObjectName("combo_t1")
        self.combo_t1.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.combo_t1)
        layer_v.addWidget(QLabel("变化期 (T2 后期):"))
        self.combo_t2 = QgsMapLayerComboBox(); self.combo_t2.setObjectName("combo_t2")
        self.combo_t2.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.combo_t2)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 差分参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("差分波段:"), 0, 0)
        self.spin_band = QSpinBox(); self.spin_band.setObjectName("spin_band")
        self.spin_band.setRange(1, 64); self.spin_band.setValue(1)
        param_grid.addWidget(self.spin_band, 0, 1)
        param_grid.addWidget(QLabel("变化灵敏度阈值:"), 1, 0)
        self.spin_thresh = QDoubleSpinBox(); self.spin_thresh.setObjectName("spin_thresh")
        self.spin_thresh.setRange(1.0, 500.0); self.spin_thresh.setValue(30.0)
        param_grid.addWidget(self.spin_thresh, 1, 1)
        self.cb_polygonize = QCheckBox("同时输出矢量斑块图层 (Shape)")
        self.cb_polygonize.setObjectName("cb_polygonize")
        self.cb_polygonize.setChecked(True)
        param_grid.addWidget(self.cb_polygonize, 2, 0, 1, 2)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行检测")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 本地双期差分检测 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_diff)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🐣 基于像元差绝对值 |T2 - T1| 提取发生变化的区域")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_diff(self):
        l1 = self.combo_t1.currentLayer()
        l2 = self.combo_t2.currentLayer()
        if not l1 or not l2:
            QMessageBox.warning(self, "提示", "请选择两期输入图层")
            return
        self.mark_run_started()
        self.status_label.setText("正在计算像元差分...")
        QApplication.processEvents()
        try:
            out_tif, out_shp = raster_diff(
                l1.source(), l2.source(), self.spin_band.value(),
                self.spin_thresh.value(), self.cb_polygonize.isChecked(),
            )
            self.status_label.setText("差分变化检测完成！")
            self.record_local_run(
                "ok", summary=f"双期像元差分检测 (阈值 {self.spin_thresh.value()})",
                output_paths=[p for p in (out_tif, out_shp) if p])
            QMessageBox.information(self, "成功", "双期像元差分检测完成！")
        except Exception as e:
            self.status_label.setText("差分检测失败")
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"计算失败: {e}")
