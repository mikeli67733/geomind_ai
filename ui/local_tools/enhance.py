# -*- coding: utf-8 -*-
"""False color composite with contrast enhancement widget."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QPushButton, QSpinBox,
    QCheckBox, QMessageBox, QGroupBox, QApplication,
)
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import image_enhance
from .base import BaseLocalToolWidget


class ImageEnhanceWidget(BaseLocalToolWidget):
    """False color composite with contrast enhancement widget."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入多波段栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. RGB 通道波段分配")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("红通道 (R):"), 0, 0)
        self.spin_r = QSpinBox(); self.spin_r.setRange(1, 64); self.spin_r.setValue(4)
        param_grid.addWidget(self.spin_r, 0, 1)
        param_grid.addWidget(QLabel("绿通道 (G):"), 1, 0)
        self.spin_g = QSpinBox(); self.spin_g.setRange(1, 64); self.spin_g.setValue(3)
        param_grid.addWidget(self.spin_g, 1, 1)
        param_grid.addWidget(QLabel("蓝通道 (B):"), 2, 0)
        self.spin_b = QSpinBox(); self.spin_b.setRange(1, 64); self.spin_b.setValue(2)
        param_grid.addWidget(self.spin_b, 2, 1)
        self.cb_stretch = QCheckBox("应用 2% ~ 98% 动态对比度拉伸增强画质")
        self.cb_stretch.setChecked(True)
        param_grid.addWidget(self.cb_stretch, 3, 0, 1, 2)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 生成增强影像")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 生成假彩色增强影像 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_enhance)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🌈 标准假彩色组合 (4-3-2) 显著突出植被覆盖")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_enhance(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择栅格图层")
            return
        self.status_label.setText("正在合成与拉伸波段...")
        QApplication.processEvents()
        try:
            image_enhance(
                layer.source(), self.spin_r.value(), self.spin_g.value(),
                self.spin_b.value(), self.cb_stretch.isChecked(),
            )
            self.status_label.setText("生成成功！已加载增强影像")
            QMessageBox.information(self, "成功", "假彩色增强影像生成完成！")
        except Exception as e:
            self.status_label.setText("生成失败")
            QMessageBox.critical(self, "错误", f"合成失败: {e}")
