# -*- coding: utf-8 -*-
"""Classification area statistics widget with table display."""
from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QLabel, QPushButton, QMessageBox, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
)
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...tools.raster_ops import area_statistics
from .base import BaseLocalToolWidget


class AreaStatisticsWidget(BaseLocalToolWidget):
    """Classification area statistics widget with table display."""

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入分类/解译栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setObjectName("layer_combo")
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        table_group = QGroupBox("2. 面积统计结果报表")
        table_v = QVBoxLayout(table_group)
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["类别编号", "像元总数", "面积 (m²)", "面积 (亩)", "占比 (%)"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table_v.addWidget(self.table)
        layout.addWidget(table_group)

        self.calc_btn = QPushButton("✨ 一键统计全图面积 (0额度)")
        self.calc_btn.setObjectName("runBtn")
        self.calc_btn.clicked.connect(self._calc_area)
        layout.addWidget(self.calc_btn)
        layout.addStretch()

    def _calc_area(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择分类图层")
            return
        self.mark_run_started()
        try:
            stats = area_statistics(layer.source())
            self.table.setRowCount(len(stats))
            for i, s in enumerate(stats):
                self.table.setItem(i, 0, QTableWidgetItem(f"类别 {s['class_id']}"))
                self.table.setItem(i, 1, QTableWidgetItem(f"{s['pixels']:,}"))
                self.table.setItem(i, 2, QTableWidgetItem(f"{s['area_m2']:,.2f}"))
                self.table.setItem(i, 3, QTableWidgetItem(f"{s['area_mu']:,.2f}"))
                self.table.setItem(i, 4, QTableWidgetItem(f"{s['percent']:.2f}%"))
            self.record_local_run(
                "ok", summary=f"面积统计完成（{len(stats)} 种地物类别）")
            QMessageBox.information(self, "统计完成", f"成功统计 {len(stats)} 种地物要素的面积分布！")
        except Exception as e:
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"统计面积异常: {e}")
