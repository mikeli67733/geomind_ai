# -*- coding: utf-8 -*-
"""
解译插件停靠面板：
- 包含软件授权激活区域 (机器码展示 + 卡密输入 + 微信联系二维码)
- 土地利用模型：显示要素多选框
- SAM3 模型：显示 Prompt 提示词输入框
兼容 PyQt5 与 PyQt6 (QGIS 3.16 ~ 3.42+)
"""

import os
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtGui import QPixmap  # <--- 用于加载二维码图片
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QPushButton, QTextEdit, QLineEdit, QProgressBar, QMessageBox,
    QGroupBox, QCheckBox, QApplication
)
from qgis.core import QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsVectorLayer, QgsRectangle
from qgis.gui import QgsMapLayerComboBox

from .extent_tool import ExtentSelectTool
from .worker import InterpretWorker
from .machine_id import get_machine_id

# PyQt5 / PyQt6 图层过滤器枚举兼容处理
RASTER_LAYER_FILTER = getattr(QgsMapLayerProxyModel, 'RasterLayer', None)
if RASTER_LAYER_FILTER is None:
    try:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.RasterLayer
    except AttributeError:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.Raster


class ImageInterpretDockWidget(QDockWidget):
    # 模型配置：(显示名称, model_key, UI模式)
    MODELS = [
        ("土地利用/多要素识别 (LANDUSE)", "LANDUSE", "landuse"),
        ("SAM3 提示词通用大模型 (SAM3)", "SAM3_MODEL", "sam3"),
    ]

    # 第一个模型对应的类别映射
    LANDUSE_CLASSES = [
        ("耕地 ", 1),
        ("林地 ", 3),
        ("草地 ", 4),
        ("建筑 ", 5),
        ("道路 ", 6),
        ("施工 ", 7),
        ("水体 ", 10),
    ]

    def __init__(self, iface, parent=None):
        super().__init__("遥感影像智能解译", parent)
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.canvas = iface.mapCanvas()
        self.settings = QSettings("ImageInterpretPlugin", "ImageInterpretPlugin")

        self.extent_tool = None
        self.selected_extent = None
        self.worker = None

        self._build_ui()
        self._load_settings()
        self._on_model_changed()  # 初始化 UI 显隐状态

    # ---------------------------------------------------------------- UI ----
    def _build_ui(self):
        container = QWidget()
        main_layout = QVBoxLayout()

        # 0. 软件授权配置 (机器码 + 卡密 + 微信二维码)
        auth_group = QGroupBox("软件授权激活")
        auth_layout = QVBoxLayout()

        # 机器码展示行
        mac_layout = QHBoxLayout()
        mac_layout.addWidget(QLabel("我的机器码:"))
        self.machine_id = get_machine_id()
        self.mac_edit = QLineEdit(self.machine_id)
        self.mac_edit.setReadOnly(True)
        mac_layout.addWidget(self.mac_edit)

        self.copy_mac_btn = QPushButton("复制")
        self.copy_mac_btn.clicked.connect(self._copy_machine_id)
        mac_layout.addWidget(self.copy_mac_btn)
        auth_layout.addLayout(mac_layout)

        # 卡密输入行
        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel("授权卡密 Key:"))
        self.license_key_edit = QLineEdit()
        self.license_key_edit.setPlaceholderText("请输入购买的卡密 (如: KEY-30D-XXXX)")
        key_layout.addWidget(self.license_key_edit)
        auth_layout.addLayout(key_layout)

        # 二维码展示行（自动读取插件目录下的 qr_code.png）
        qr_layout = QVBoxLayout()
        qr_label = QLabel()
        qr_path = os.path.join(self.plugin_dir, "qr_code.png")

        if os.path.exists(qr_path):
            pixmap = QPixmap(qr_path).scaled(130, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            qr_label.setPixmap(pixmap)
        else:
            qr_label.setText("[扫码购买卡密]\n(请将二维码保存为 qr_code.png 放入插件目录)")
            qr_label.setStyleSheet("color: #888888; border: 1px dashed #CCCCCC; padding: 10px;")

        qr_label.setAlignment(Qt.AlignCenter)
        qr_layout.addWidget(qr_label)

        tip_label = QLabel("扫码添加作者QQ / 购买授权卡密")
        tip_label.setAlignment(Qt.AlignCenter)
        tip_label.setStyleSheet("color: #555555; font-size: 11px;")
        qr_layout.addWidget(tip_label)

        auth_layout.addLayout(qr_layout)
        auth_group.setLayout(auth_layout)
        main_layout.addWidget(auth_group)

        # 服务器配置
        server_group = QGroupBox("服务器配置")
        server_layout = QHBoxLayout()
        server_layout.addWidget(QLabel("服务地址:"))
        self.server_url_edit = QLineEdit()
        self.server_url_edit.setPlaceholderText("https://application-showed-revolutionary-flooring.trycloudflare.com")
        server_layout.addWidget(self.server_url_edit)
        server_group.setLayout(server_layout)
        main_layout.addWidget(server_group)

        # 1. 影像图层选择
        layer_group = QGroupBox("1. 选择栅格图层")
        layer_layout = QVBoxLayout()
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_layout.addWidget(self.layer_combo)
        layer_group.setLayout(layer_layout)
        main_layout.addWidget(layer_group)

        # 2. 选择解译模型
        model_group = QGroupBox("2. 选择解译模型/任务")
        model_layout = QVBoxLayout()
        self.model_combo = QComboBox()
        for label, model_key, mode in self.MODELS:
            self.model_combo.addItem(label, userData={"key": model_key, "mode": mode})
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        model_layout.addWidget(self.model_combo)
        model_group.setLayout(model_layout)
        main_layout.addWidget(model_group)

        # 3. 动态配置区域：土地利用分类多选框 (仅 LANDUSE 模型显示)
        self.class_group = QGroupBox("3. 选择要解译的要素类别 (可多选)")
        class_grid = QGridLayout()
        self.class_checkboxes = {}
        for idx, (label, cls_id) in enumerate(self.LANDUSE_CLASSES):
            cb = QCheckBox(label)
            if cls_id == 10:  # 默认勾选水体
                cb.setChecked(True)
            self.class_checkboxes[cls_id] = cb
            class_grid.addWidget(cb, idx // 3, idx % 3)
        self.class_group.setLayout(class_grid)
        main_layout.addWidget(self.class_group)

        # 4. 动态配置区域：SAM3 提示词输入框 (仅 SAM3 模型显示)
        self.prompt_group = QGroupBox("3. 输入 SAM 提示词 (Prompt)")
        prompt_layout = QVBoxLayout()
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("例如: water, floodwater (支持英文单词描述)")
        self.prompt_edit.setFixedHeight(70)
        prompt_layout.addWidget(self.prompt_edit)
        self.prompt_group.setLayout(prompt_layout)
        main_layout.addWidget(self.prompt_group)

        # 5. 框选范围
        extent_group = QGroupBox("4. 框选解译范围")
        extent_layout = QVBoxLayout()
        btn_row = QHBoxLayout()
        self.select_extent_btn = QPushButton("地图拖拽框选")
        self.select_extent_btn.clicked.connect(self._activate_extent_tool)
        btn_row.addWidget(self.select_extent_btn)

        self.use_canvas_extent_btn = QPushButton("当前视图范围")
        self.use_canvas_extent_btn.clicked.connect(self._use_canvas_extent)
        btn_row.addWidget(self.use_canvas_extent_btn)
        extent_layout.addLayout(btn_row)

        self.extent_label = QLabel("尚未选择解译范围")
        self.extent_label.setWordWrap(True)
        self.extent_label.setStyleSheet("color: gray;")
        extent_layout.addWidget(self.extent_label)
        extent_group.setLayout(extent_layout)
        main_layout.addWidget(extent_group)

        # 6. 执行解译
        run_group = QGroupBox("5. 执行任务")
        run_layout = QVBoxLayout()
        self.run_btn = QPushButton("开始智能解译")
        self.run_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.run_btn.clicked.connect(self._run_interpret)
        run_layout.addWidget(self.run_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        run_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        run_layout.addWidget(self.status_label)

        run_group.setLayout(run_layout)
        main_layout.addWidget(run_group)

        main_layout.addStretch()
        container.setLayout(main_layout)
        self.setWidget(container)

    # -------------------------------------------------------- UI 交互逻辑 ----
    def _copy_machine_id(self):
        QApplication.clipboard().setText(self.machine_id)
        QMessageBox.information(self, "提示", "机器码已复制到剪贴板！")

    def _on_model_changed(self):
        """模型切换时，动态切换显示『多选复选框』或『Prompt输入框』"""
        data = self.model_combo.currentData()
        mode = data.get("mode", "single") if data else "single"

        if mode == "landuse":
            self.class_group.setVisible(True)
            self.prompt_group.setVisible(False)
        elif mode == "sam3":
            self.class_group.setVisible(False)
            self.prompt_group.setVisible(True)
        else:
            self.class_group.setVisible(False)
            self.prompt_group.setVisible(False)

    def _load_settings(self):
        url = self.settings.value("server_url", "http://127.0.0.1:8000")
        key = self.settings.value("license_key", "")
        self.server_url_edit.setText(url)
        self.license_key_edit.setText(key)

    def _save_settings(self):
        self.settings.setValue("server_url", self.server_url_edit.text().strip())
        self.settings.setValue("license_key", self.license_key_edit.text().strip())

    # ------------------------------------------------------------- 范围选择 ---
    def _activate_extent_tool(self):
        self.extent_tool = ExtentSelectTool(self.canvas)
        self.extent_tool.extentSelected.connect(self._on_extent_selected)
        self.canvas.setMapTool(self.extent_tool)
        self.status_label.setText("请在地图上按住左键拖拽框选范围（右键/Esc取消）")

    def _use_canvas_extent(self):
        rect = self.canvas.extent()
        self._on_extent_selected(rect)

    def _on_extent_selected(self, rect: QgsRectangle):
        self.selected_extent = rect
        self.extent_label.setText(
            f"范围: X[{rect.xMinimum():.2f}, {rect.xMaximum():.2f}]  "
            f"Y[{rect.yMinimum():.2f}, {rect.yMaximum():.2f}]  "
            f"(CRS: {self.canvas.mapSettings().destinationCrs().authid()})"
        )
        self.extent_label.setStyleSheet("color: black;")
        self.status_label.setText("已选定范围，准备执行解译")

    # --------------------------------------------------------------- 运行 ----
    def _run_interpret(self):
        server_url = self.server_url_edit.text().strip()
        if not server_url:
            QMessageBox.warning(self, "提示", "请填写服务器地址")
            return

        license_key = self.license_key_edit.text().strip()
        if not license_key:
            QMessageBox.warning(self, "提示", "请输入授权卡密 Key")
            return

        layer = self.layer_combo.currentLayer()
        if layer is None or not isinstance(layer, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "请选择有效的栅格影像图层")
            return

        if self.selected_extent is None:
            QMessageBox.warning(self, "提示", "请框选解译范围")
            return

        model_data = self.model_combo.currentData()
        model_key = model_data.get("key")
        mode = model_data.get("mode")

        prompt = ""
        target_class = ""

        if mode == "landuse":
            selected_ids = [str(cls_id) for cls_id, cb in self.class_checkboxes.items() if cb.isChecked()]
            if not selected_ids:
                QMessageBox.warning(self, "提示", "请至少勾选一个解译要素类别（如水体、耕地等）")
                return
            target_class = ",".join(selected_ids)
        elif mode == "sam3":
            prompt = self.prompt_edit.toPlainText().strip()
            if not prompt:
                QMessageBox.warning(self, "提示", "使用 SAM3 模型时必须输入提示词 (Prompt)")
                return

        self._save_settings()

        extent_in_layer_crs = self._transform_extent_to_layer_crs(self.selected_extent, layer)

        self.run_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.status_label.setText("正在后台处理，请稍候...")

        self.worker = InterpretWorker(
            raster_layer=layer,
            extent=extent_in_layer_crs,
            model_key=model_key,
            target_class=target_class,
            prompt=prompt,
            server_url=server_url,
            license_key=license_key,
            machine_id=self.machine_id
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._on_finished_ok)
        self.worker.finished_error.connect(self._on_finished_error)
        self.worker.start()

    def _transform_extent_to_layer_crs(self, rect, layer):
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()
        if canvas_crs == layer_crs:
            return rect
        from qgis.core import QgsCoordinateTransform
        transform = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
        return transform.transformBoundingBox(rect)

    def _on_progress(self, text):
        self.status_label.setText(text)

    def _on_finished_ok(self, result_path, content_type):
        self.run_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

        time_display = datetime.now().strftime("%H:%M:%S")
        model_label = self.model_combo.currentText().split(' ')[0]
        layer_name = f"解译结果_{model_label}({time_display})"

        if result_path.endswith('.tif'):
            new_layer = QgsRasterLayer(result_path, layer_name)
        else:
            new_layer = QgsVectorLayer(result_path, layer_name, "ogr")

        if not new_layer.isValid():
            QMessageBox.critical(self, "错误", "结果加载失败，返回的文件损坏或格式受限")
            self.status_label.setText("加载结果失败")
            return

        QgsProject.instance().addMapLayer(new_layer)
        self.status_label.setText(f"完成！已加载图层: {layer_name}")

    def _on_finished_error(self, error_msg):
        self.run_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_label.setText("解译失败")
        QMessageBox.critical(self, "解译失败", error_msg)
