# -*- coding: utf-8 -*-
"""Spectral index calculator (NDVI, NDWI, EVI, SAVI, etc.)."""
import os
import tempfile

from osgeo import gdal

from qgis.PyQt.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QPushButton, QSpinBox, QDoubleSpinBox, QCheckBox,
    QMessageBox, QGroupBox, QApplication,
)
from qgis.core import QgsProject, QgsRasterLayer, QgsCoordinateTransform
from qgis.gui import QgsMapLayerComboBox

from ...core.compat import RASTER_LAYER_FILTER
from ...utils.extent_tool import ExtentSelectTool
from ...tools.raster_ops import calc_spectral_index
from .base import BaseLocalToolWidget


class SpectralIndexTaskWidget(BaseLocalToolWidget):
    """Spectral index calculator (NDVI, NDWI, EVI, SAVI, etc.)."""

    INDEX_CONFIG = {
        "ndvi":  {"b1_label": "近红外 (NIR):", "b2_label": "红光 (Red):",   "threshold": 0.2,  "needs_b3": False},
        "gndvi": {"b1_label": "近红外 (NIR):", "b2_label": "绿光 (Green):", "threshold": 0.2,  "needs_b3": False},
        "savi":  {"b1_label": "近红外 (NIR):", "b2_label": "红光 (Red):",   "threshold": 0.15, "needs_b3": False},
        "evi":   {"b1_label": "近红外 (NIR):", "b2_label": "红光 (Red):",   "threshold": 0.2,  "needs_b3": True,  "b3_label": "蓝光 (Blue):"},
        "fvc":   {"b1_label": "近红外 (NIR):", "b2_label": "红光 (Red):",   "threshold": 0.2,  "needs_b3": False},
        "ndwi":  {"b1_label": "绿光 (Green):", "b2_label": "近红外 (NIR):", "threshold": 0.0,  "needs_b3": False},
        "mndwi": {"b1_label": "绿光 (Green):", "b2_label": "短波红外 (SWIR):", "threshold": 0.0, "needs_b3": False},
        "ndbi":  {"b1_label": "短波红外 (SWIR):", "b2_label": "近红外 (NIR):", "threshold": 0.0, "needs_b3": False},
        "ndmi":  {"b1_label": "近红外 (NIR):", "b2_label": "短波红外 (SWIR):", "threshold": 0.1, "needs_b3": False},
        "nbr":   {"b1_label": "近红外 (NIR):", "b2_label": "短波红外 (SWIR):", "threshold": 0.1, "needs_b3": False},
        "bsi":   {"b1_label": "短波红外 (SWIR):", "b2_label": "红光 (Red):", "threshold": 0.2, "needs_b3": True, "b3_label": "近红外 (NIR):"},
    }

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # Layer selection
        layer_group = QGroupBox("1. 输入多波段栅格图层")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setObjectName("layer_combo")
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        # Parameters
        param_group = QGroupBox("2. 指数类型与波段配置")
        param_grid = QGridLayout(param_group)

        param_grid.addWidget(QLabel("计算指数:"), 0, 0)
        self.index_combo = QComboBox()
        self.index_combo.setObjectName("index_combo")
        self.index_combo.addItem("🍀 NDVI (归一化植被指数)", "ndvi")
        self.index_combo.addItem("🍃 GNDVI (绿度植被指数)", "gndvi")
        self.index_combo.addItem("🌱 SAVI (土壤调节植被指数)", "savi")
        self.index_combo.addItem("🌲 EVI (增强型植被指数)", "evi")
        self.index_combo.addItem("🌾 FVC (植被覆盖度-像元二分法)", "fvc")
        self.index_combo.addItem("💧 NDWI (水体指数-McFeeters)", "ndwi")
        self.index_combo.addItem("💧 MNDWI (改进水体指数-Xu)", "mndwi")
        self.index_combo.addItem("🏡 NDBI (归一化建筑指数)", "ndbi")
        self.index_combo.addItem("🍂 NDMI (地表湿度水分指数)", "ndmi")
        self.index_combo.addItem("🏜️ BSI (裸土裸地指数)", "bsi")
        self.index_combo.addItem("🔥 NBR (林火燃烧面积指数)", "nbr")
        self.index_combo.currentIndexChanged.connect(self._on_index_type_changed)
        param_grid.addWidget(self.index_combo, 0, 1)

        self.lbl_b1 = QLabel("近红外 (NIR):")
        self.spin_b1 = QSpinBox(); self.spin_b1.setObjectName("spin_b1")
        self.spin_b1.setRange(1, 64); self.spin_b1.setValue(4)
        param_grid.addWidget(self.lbl_b1, 1, 0); param_grid.addWidget(self.spin_b1, 1, 1)

        self.lbl_b2 = QLabel("红光 (Red):")
        self.spin_b2 = QSpinBox(); self.spin_b2.setObjectName("spin_b2")
        self.spin_b2.setRange(1, 64); self.spin_b2.setValue(3)
        param_grid.addWidget(self.lbl_b2, 2, 0); param_grid.addWidget(self.spin_b2, 2, 1)

        self.lbl_b3 = QLabel("蓝光 (Blue):")
        self.spin_b3 = QSpinBox(); self.spin_b3.setObjectName("spin_b3")
        self.spin_b3.setRange(1, 64); self.spin_b3.setValue(1)
        self.lbl_b3.setVisible(False); self.spin_b3.setVisible(False)
        param_grid.addWidget(self.lbl_b3, 3, 0); param_grid.addWidget(self.spin_b3, 3, 1)

        self.cb_threshold = QCheckBox("启用阈值二值化提取:")
        self.cb_threshold.setObjectName("cb_threshold")
        self.spin_threshold = QDoubleSpinBox()
        self.spin_threshold.setObjectName("spin_threshold")
        self.spin_threshold.setRange(-1.0, 1.0)
        self.spin_threshold.setSingleStep(0.05)
        self.spin_threshold.setValue(0.2)
        self.spin_threshold.setEnabled(False)
        self.cb_threshold.toggled.connect(self.spin_threshold.setEnabled)
        param_grid.addWidget(self.cb_threshold, 4, 0)
        param_grid.addWidget(self.spin_threshold, 4, 1)
        layout.addWidget(param_group)

        # Extent
        extent_group = QGroupBox("3. 计算范围 (留空计算全图)")
        extent_layout = QVBoxLayout(extent_group)
        btn_row = QHBoxLayout()
        self.select_extent_btn = QPushButton("🐾 框选范围")
        self.select_extent_btn.clicked.connect(self._activate_extent_tool)
        btn_row.addWidget(self.select_extent_btn)
        self.use_canvas_extent_btn = QPushButton("🌸 当前视图")
        self.use_canvas_extent_btn.clicked.connect(self._use_canvas_extent)
        btn_row.addWidget(self.use_canvas_extent_btn)
        extent_layout.addLayout(btn_row)
        self.extent_label = QLabel("默认计算整幅栅格影像")
        self.extent_label.setObjectName("extentLabel")
        extent_layout.addWidget(self.extent_label)
        layout.addWidget(extent_group)

        # Run
        run_group = QGroupBox("4. 本地计算")
        run_layout = QVBoxLayout(run_group)
        self.calc_btn = QPushButton("✨ 本地极速计算 (0额度)")
        self.calc_btn.setObjectName("runBtn")
        self.calc_btn.clicked.connect(self._run_calculation)
        run_layout.addWidget(self.calc_btn)
        self.status_label = QLabel("🍡 纯本地算法运行，免联网、秒级计算")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group)
        layout.addStretch()
        self._on_layer_changed()

    def _on_index_type_changed(self):
        idx_type = self.index_combo.currentData()
        cfg = self.INDEX_CONFIG.get(idx_type, {})
        self.lbl_b3.setVisible(False); self.spin_b3.setVisible(False)
        self.lbl_b1.setText(cfg.get("b1_label", "波段 1:"))
        self.lbl_b2.setText(cfg.get("b2_label", "波段 2:"))
        self.spin_threshold.setValue(cfg.get("threshold", 0.2))
        if cfg.get("needs_b3"):
            self.lbl_b3.setText(cfg.get("b3_label", "波段 3:"))
            self.lbl_b3.setVisible(True); self.spin_b3.setVisible(True)

    def _on_layer_changed(self):
        layer = self.layer_combo.currentLayer()
        if layer and isinstance(layer, QgsRasterLayer):
            band_count = layer.bandCount()
            for sp in [self.spin_b1, self.spin_b2, self.spin_b3]:
                sp.setMaximum(band_count)
            if band_count >= 4:
                self.spin_b1.setValue(4); self.spin_b2.setValue(3); self.spin_b3.setValue(1)
            elif band_count >= 2:
                self.spin_b1.setValue(2); self.spin_b2.setValue(1)

    def _activate_extent_tool(self):
        self.extent_tool = ExtentSelectTool(self.canvas)
        self.extent_tool.extentSelected.connect(self._on_extent_selected)
        self.canvas.setMapTool(self.extent_tool)
        self.status_label.setText("请在地图上按住左键拖拽框选范围")

    def _use_canvas_extent(self):
        self._on_extent_selected(self.canvas.extent())

    def _on_extent_selected(self, rect):
        self.selected_extent = rect
        crs_id = self.canvas.mapSettings().destinationCrs().authid()
        self.extent_label.setText(
            f"X: [{rect.xMinimum():.2f}, {rect.xMaximum():.2f}]\n"
            f"Y: [{rect.yMinimum():.2f}, {rect.yMaximum():.2f}]\nCRS: {crs_id}"
        )
        self.status_label.setText("已选定局部计算范围")

    def _run_calculation(self):
        layer = self.layer_combo.currentLayer()
        if not layer or not isinstance(layer, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "请选择有效的栅格图层")
            return

        source_path = layer.source()
        if not os.path.exists(source_path):
            QMessageBox.warning(self, "提示", "无法读取图层源文件路径")
            return

        self.mark_run_started()
        idx_type = self.index_combo.currentData()
        idx_name = self.index_combo.currentText().split(" ")[0]
        threshold = self.spin_threshold.value() if self.cb_threshold.isChecked() else None

        self.status_label.setText("正在计算栅格指数...")
        QApplication.processEvents()

        try:
            # Crop to extent if selected
            read_path = source_path
            if self.selected_extent:
                canvas_crs = self.canvas.mapSettings().destinationCrs()
                layer_crs = layer.crs()
                ext = self.selected_extent
                if canvas_crs != layer_crs:
                    tr = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
                    ext = tr.transformBoundingBox(ext)
                temp_crop = os.path.join(tempfile.gettempdir(), f"src_{id(self)}.tif")
                proj_win = [ext.xMinimum(), ext.yMaximum(), ext.xMaximum(), ext.yMinimum()]
                ds_crop = gdal.Translate(temp_crop, source_path, projWin=proj_win)
                ds_crop = None
                read_path = temp_crop

            out_path = calc_spectral_index(
                read_path, idx_type, self.spin_b1.value(),
                self.spin_b2.value(), self.spin_b3.value(), threshold,
            )
            self.status_label.setText(f"计算完成！已加载 {idx_name} 图层")
            self.record_local_run(
                "ok", summary=f"{idx_name} 指数计算完成", output_paths=[out_path])
            QMessageBox.information(self, "成功", "指数计算完成，图层已加载！")
        except Exception as e:
            self.status_label.setText("计算出错")
            self.record_local_run("failed", error=str(e))
            QMessageBox.critical(self, "错误", f"指数计算失败: {e}")
