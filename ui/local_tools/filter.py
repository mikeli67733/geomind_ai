# -*- coding: utf-8 -*-
"""Spatial filter widget (sobel, gaussian, laplacian)."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QComboBox, QPushButton,
    QSpinBox, QMessageBox, QGroupBox, QApplication,
)
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import spatial_filter
from .base import BaseLocalToolWidget


class SpatialFilterWidget(BaseLocalToolWidget):
    """Spatial filter widget (sobel, gaussian, laplacian)."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setObjectName("layer_combo")
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 滤波与算子设置")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("选择算子:"), 0, 0)
        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("filter_combo")
        self.filter_combo.addItem("🔎 Sobel 边缘梯度提取 (道路/地界识别)", "sobel")
        self.filter_combo.addItem("🫧 高斯平滑降噪 (Gaussian Blur)", "gaussian")
        self.filter_combo.addItem("🪄 拉普拉斯高通锐化 (Sharpening)", "laplacian")
        param_grid.addWidget(self.filter_combo, 0, 1)
        param_grid.addWidget(QLabel("波段序号:"), 1, 0)
        self.spin_band = QSpinBox(); self.spin_band.setObjectName("spin_band")
        self.spin_band.setRange(1, 64); self.spin_band.setValue(1)
        param_grid.addWidget(self.spin_band, 1, 1)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行滤波")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 本地执行空间滤波 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_filter)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🎀 采用 2D 卷积提取地表空间纹理与线性边缘特征")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_filter(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择栅格图层")
            return
        self.mark_run_started()
        f_type = self.filter_combo.currentData()
        f_label = self.filter_combo.currentText().split(" ")[0]
        self.status_label.setText("正在执行空间滤波卷积...")
        QApplication.processEvents()
        try:
            out_path = spatial_filter(layer.source(), f_type, self.spin_band.value())
            self.status_label.setText("滤波处理完成！图层已加载")
            self.record_local_run(
                "ok", summary=f"空间滤波 [{f_label}]", output_paths=[out_path])
            QMessageBox.information(self, "成功", "空间滤波与边缘提取完成！")
        except Exception as e:
            self.status_label.setText("滤波失败")
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"计算失败: {e}")
