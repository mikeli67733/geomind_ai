# -*- coding: utf-8 -*-
"""PCA principal component analysis widget."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QSpinBox, QPushButton,
    QMessageBox, QGroupBox, QApplication,
)
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import run_pca
from .base import BaseLocalToolWidget


class PcaTransformWidget(BaseLocalToolWidget):
    """PCA principal component analysis widget."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入多波段栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setObjectName("layer_combo")
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. PCA 变换设置")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("输出主成分数量:"), 0, 0)
        self.spin_comp = QSpinBox(); self.spin_comp.setObjectName("spin_comp")
        self.spin_comp.setRange(1, 6); self.spin_comp.setValue(3)
        param_grid.addWidget(self.spin_comp, 0, 1)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行变换")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 执行 PCA 主成分变换 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_pca)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🔮 提取前 N 个主成分 (PC1/PC2/PC3)，消除波段冗余信息")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_pca(self):
        layer = self.layer_combo.currentLayer()
        if not layer or layer.bandCount() < 2:
            QMessageBox.warning(self, "提示", "请选择至少包含 2 个波段的栅格图层")
            return
        self.mark_run_started()
        n_comp = self.spin_comp.value()
        self.status_label.setText("正在执行主成分正交变换...")
        QApplication.processEvents()
        try:
            out_path = run_pca(layer.source(), n_comp)
            self.status_label.setText(f"PCA 变换完成！已输出 {n_comp} 个主成分图层")
            self.record_local_run(
                "ok", summary=f"PCA 主成分分析 ({n_comp} 个主成分)", output_paths=[out_path])
            QMessageBox.information(self, "成功", f"成功生成 {n_comp} 个主成分特征波段！")
        except Exception as e:
            self.status_label.setText("PCA 计算失败")
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"PCA 变换失败: {e}")
