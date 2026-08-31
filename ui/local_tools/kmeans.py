# -*- coding: utf-8 -*-
"""K-Means unsupervised clustering widget."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QPushButton, QSpinBox,
    QMessageBox, QGroupBox, QApplication,
)
from qgis.core import QgsRasterLayer
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import kmeans_cluster
from .base import BaseLocalToolWidget


class KMeansClusterWidget(BaseLocalToolWidget):
    """K-Means unsupervised clustering widget."""

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

        param_group = QGroupBox("2. K-Means 聚类参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("地物聚类类别数 (K):"), 0, 0)
        self.spin_k = QSpinBox(); self.spin_k.setObjectName("spin_k")
        self.spin_k.setRange(2, 15); self.spin_k.setValue(5)
        param_grid.addWidget(self.spin_k, 0, 1)
        param_grid.addWidget(QLabel("最大迭代次数:"), 1, 0)
        self.spin_iter = QSpinBox(); self.spin_iter.setObjectName("spin_iter")
        self.spin_iter.setRange(5, 50); self.spin_iter.setValue(15)
        param_grid.addWidget(self.spin_iter, 1, 1)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行无监督聚类")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 开始本地聚类分类 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_kmeans)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🍭 纯本地高速聚类算法，自动划分地物类别图斑")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_kmeans(self):
        layer = self.layer_combo.currentLayer()
        if not layer or not isinstance(layer, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "请选择有效的栅格图层")
            return
        self.mark_run_started()
        k = self.spin_k.value()
        self.status_label.setText("正在执行像元聚类中...")
        QApplication.processEvents()
        try:
            out_path = kmeans_cluster(layer.source(), k, self.spin_iter.value())
            self.status_label.setText(f"聚类完成！共生成 {k} 个地物类别")
            self.record_local_run(
                "ok", summary=f"K-Means 聚类 (K={k})", output_paths=[out_path])
            QMessageBox.information(self, "成功", "K-Means 聚类成功！")
        except Exception as e:
            self.status_label.setText("聚类失败")
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"K-Means 执行异常: {e}")
