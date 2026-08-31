# -*- coding: utf-8 -*-
"""Vector geometry simplify and smooth widget."""
from datetime import datetime

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QDoubleSpinBox, QSpinBox, QMessageBox, QGroupBox, QApplication,
)
from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import VECTOR_LAYER_FILTER
from ...tools.vector_ops import vector_simplify_and_smooth
from .base import BaseLocalToolWidget


class VectorSmoothSimplifyWidget(BaseLocalToolWidget):
    """Vector geometry simplify and smooth widget."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入待处理矢量图层")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setObjectName("layer_combo")
        self.layer_combo.setFilters(VECTOR_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 几何精修参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("化简容差距离 (米):"), 0, 0)
        self.spin_tol = QDoubleSpinBox(); self.spin_tol.setObjectName("spin_tol")
        self.spin_tol.setRange(0.01, 100.0); self.spin_tol.setValue(1.0)
        param_grid.addWidget(self.spin_tol, 0, 1)
        param_grid.addWidget(QLabel("平滑迭代次数:"), 1, 0)
        self.spin_iter = QSpinBox(); self.spin_iter.setObjectName("spin_iter")
        self.spin_iter.setRange(1, 10); self.spin_iter.setValue(2)
        param_grid.addWidget(self.spin_iter, 1, 1)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行精修")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 一键化简与平滑图斑 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_simplify)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🪄 去除 AI 提取产生的锯齿边缘，输出规整平滑的边界")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_simplify(self):
        layer = self.layer_combo.currentLayer()
        if not layer or not isinstance(layer, QgsVectorLayer):
            QMessageBox.warning(self, "提示", "请选择有效的矢量图层")
            return
        self.mark_run_started()
        self.status_label.setText("正在执行几何化简与平滑...")
        QApplication.processEvents()
        try:
            out_layer = vector_simplify_and_smooth(layer, self.spin_tol.value(), self.spin_iter.value())
            time_str = datetime.now().strftime("%H:%M:%S")
            out_layer.setName(f"{layer.name()}_精修平滑({time_str})")
            QgsProject.instance().addMapLayer(out_layer)
            self.status_label.setText("几何精修完成！已加载图层")
            self.record_local_run(
                "ok", summary=f"矢量化简平滑（容差 {self.spin_tol.value()} m）")
            QMessageBox.information(self, "成功", "图斑化简与平滑处理完成！")
        except Exception as e:
            self.status_label.setText("处理失败")
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"矢量精修异常: {e}")
