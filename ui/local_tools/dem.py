# -*- coding: utf-8 -*-
"""DEM terrain analysis widget (hillshade, slope, aspect, TRI)."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QComboBox, QPushButton,
    QDoubleSpinBox, QMessageBox, QGroupBox, QApplication,
)
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import dem_analysis
from .base import BaseLocalToolWidget


class DemAnalysisWidget(BaseLocalToolWidget):
    """DEM terrain analysis widget (hillshade, slope, aspect, TRI)."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入高程 DEM 栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 地形分析类型")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("分析功能:"), 0, 0)
        self.dem_type_combo = QComboBox()
        self.dem_type_combo.addItem("🗻 山体阴影 (Hillshade - 三维光照判读)", "hillshade")
        self.dem_type_combo.addItem("📐 坡度分析 (Slope - 度数度量)", "slope")
        self.dem_type_combo.addItem("🧭 坡向分析 (Aspect - 方位角)", "aspect")
        self.dem_type_combo.addItem("📈 地形起伏度 (TRI - 地形粗糙度)", "TRI")
        param_grid.addWidget(self.dem_type_combo, 0, 1)
        param_grid.addWidget(QLabel("Z 轴高程缩放系数:"), 1, 0)
        self.spin_z = QDoubleSpinBox(); self.spin_z.setRange(0.1, 100.0); self.spin_z.setValue(1.0)
        param_grid.addWidget(self.spin_z, 1, 1)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行地形分析")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 本地地形快速提取 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_dem)
        run_layout.addWidget(self.run_btn)
        self.status_label = QLabel("🧁 基于 GDAL DEM 核心算法，秒级生成地形产品")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group); layout.addStretch()

    def _run_dem(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择 DEM 栅格图层")
            return
        dem_type = self.dem_type_combo.currentData()
        name_label = self.dem_type_combo.currentText().split(" ")[0]
        self.status_label.setText("正在计算地形特征...")
        QApplication.processEvents()
        try:
            dem_analysis(layer.source(), dem_type, self.spin_z.value())
            self.status_label.setText("地形分析完成！图层已加载")
            QMessageBox.information(self, "成功", f"地形分析 [{name_label}] 完成！")
        except Exception as e:
            self.status_label.setText("地形分析失败")
            QMessageBox.critical(self, "错误", f"DEM 分析失败: {e}")
