# -*- coding: utf-8 -*-
"""
遥感智能解译停靠面板 (AI 深度解译 + 本地免费遥感全能工具箱)
- Page 0: 首页 (分门别类导航入口)
- Page 1: 账号与设置中心
- Page 2+: AI 专项解译大模型 (土地利用 / 建筑 / 水体 / 林草 / 耕地 / 道路 / SAM3 / 变化检测)
- Page 9+: 10 大本地免费遥感与 GIS 工具
"""

import os
import tempfile
from datetime import datetime

import numpy as np
from osgeo import gdal, ogr, osr

from qgis.PyQt.QtCore import Qt, QSettings, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QPushButton, QTextEdit, QLineEdit, QProgressBar, QMessageBox,
    QGroupBox, QCheckBox, QApplication, QToolButton, QStackedWidget, QScrollArea,
    QFrame, QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QHeaderView
)
from qgis.core import (
    QgsProject, QgsMapLayerProxyModel, QgsRasterLayer, QgsVectorLayer,
    QgsRectangle, QgsCoordinateTransform, QgsApplication
)
from qgis.gui import QgsMapLayerComboBox

from .extent_tool import ExtentSelectTool
from .interpret_task import InterpretTask
from .machine_id import get_machine_id
from .auth_client import GeoMindAuthClient, AuthApiError
from .login_dialog import LoginDialog
from .plan_dialog import PlanDialog
from .constants import (
    MODELS, LANDUSE_CLASSES, DEFAULT_CHECKED_CLASS_IDS,
    DEFAULT_SERVER_URL, SETTINGS_ORG, SETTINGS_APP,
    SETTINGS_KEY_SERVER_URL, SETTINGS_KEY_TOKEN, SETTINGS_KEY_USERNAME,
    PLAN_LABELS, FREE_PLAN_DAILY_QUOTA, fetch_remote_server_url
)

# 栅格与矢量图层过滤器枚举兼容
RASTER_LAYER_FILTER = getattr(QgsMapLayerProxyModel, 'RasterLayer', None)
if RASTER_LAYER_FILTER is None:
    try:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.RasterLayer
    except AttributeError:
        RASTER_LAYER_FILTER = QgsMapLayerProxyModel.Filter.Raster

VECTOR_LAYER_FILTER = getattr(QgsMapLayerProxyModel, 'VectorLayer', None)
if VECTOR_LAYER_FILTER is None:
    try:
        VECTOR_LAYER_FILTER = QgsMapLayerProxyModel.Filter.VectorLayer
    except AttributeError:
        VECTOR_LAYER_FILTER = QgsMapLayerProxyModel.Filter.Vector


def get_model_key_by_mode(target_mode: str, fallback_key: str = "") -> str:
    """动态从 constants.MODELS 中查找后端真实支持的 model_key"""
    for item in MODELS:
        if len(item) >= 3 and item[2] == target_mode:
            return item[1]
    if MODELS and len(MODELS[0]) >= 2:
        return MODELS[0][1]
    return fallback_key


def find_class_ids_by_keywords(keywords: list, fallback_id: str = "") -> str:
    """
    根据关键词从 constants.LANDUSE_CLASSES 中动态查找对应的类别 ID
    确保无论常数表如何调整，均可自动匹配
    """
    matched = []
    for label, cls_id in LANDUSE_CLASSES:
        for kw in keywords:
            if kw in label:
                matched.append(str(cls_id))
                break
    return ",".join(matched) if matched else fallback_id


# =========================================================================
# 一、 AI 任务基类 (BaseTaskWidget)
# =========================================================================
class BaseTaskWidget(QWidget):
    def __init__(self, main_dock, model_key: str, mode: str, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self.canvas = main_dock.canvas
        self.model_key = model_key
        self.mode = mode

        self.extent_tool = None
        self.selected_extent = None
        self.task = None
        self._build_base_ui()

    def _build_base_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入影像")
        layer_layout = QVBoxLayout(layer_group)
        self.lbl_t1 = QLabel("基准期 / 目标影像 (T1):")
        self.layer_combo_t1 = QgsMapLayerComboBox()
        self.layer_combo_t1.setFilters(RASTER_LAYER_FILTER)
        layer_layout.addWidget(self.lbl_t1)
        layer_layout.addWidget(self.layer_combo_t1)

        self.custom_layer_layout = QVBoxLayout()
        layer_layout.addLayout(self.custom_layer_layout)
        layout.addWidget(layer_group)

        self.param_group = QGroupBox("2. 任务参数配置")
        self.param_layout = QVBoxLayout(self.param_group)
        self.build_parameters_ui(self.param_layout)
        layout.addWidget(self.param_group)

        extent_group = QGroupBox("3. 框选解译范围")
        extent_layout = QVBoxLayout(extent_group)
        btn_row = QHBoxLayout()
        self.select_extent_btn = QPushButton("🐾 拖拽框选范围")
        self.select_extent_btn.clicked.connect(self._activate_extent_tool)
        btn_row.addWidget(self.select_extent_btn)

        self.use_canvas_extent_btn = QPushButton("🌸 当前视图范围")
        self.use_canvas_extent_btn.clicked.connect(self._use_canvas_extent)
        btn_row.addWidget(self.use_canvas_extent_btn)
        extent_layout.addLayout(btn_row)

        self.extent_label = QLabel("尚未选择解译范围")
        self.extent_label.setObjectName("extentLabel")
        self.extent_label.setWordWrap(True)
        extent_layout.addWidget(self.extent_label)
        layout.addWidget(extent_group)

        run_group = QGroupBox("4. 执行任务")
        run_layout = QVBoxLayout(run_group)
        run_btn_row = QHBoxLayout()
        self.run_btn = QPushButton("✨ 开始智能解译")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_task)
        run_btn_row.addWidget(self.run_btn, stretch=2)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.clicked.connect(self._cancel_task)
        self.cancel_btn.setEnabled(False)
        run_btn_row.addWidget(self.cancel_btn, stretch=1)
        run_layout.addLayout(run_btn_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        run_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group)
        layout.addStretch()

    def build_parameters_ui(self, layout: QVBoxLayout):
        pass

    def get_task_parameters(self) -> dict:
        return {}

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
        crs_id = self.canvas.mapSettings().destinationCrs().authid()
        self.extent_label.setText(
            f"X: [{rect.xMinimum():.2f}, {rect.xMaximum():.2f}]\n"
            f"Y: [{rect.yMinimum():.2f}, {rect.yMaximum():.2f}]\n"
            f"CRS: {crs_id}"
        )
        self.status_label.setText("已选定解译范围")

    def _run_task(self):
        if self.task is not None:
            QMessageBox.information(self, "提示", "已有任务正在运行，请先取消或等待完成")
            return

        server_url = self.dock.current_server_url()
        token = self.dock.token
        if not token:
            QMessageBox.warning(self, "提示", "请先登录账号后再执行解译")
            self.dock.show_account_page()
            return

        layer_t1 = self.layer_combo_t1.currentLayer()
        if layer_t1 is None or not isinstance(layer_t1, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "请选择有效的基准期 T1 栅格图层")
            return

        if self.selected_extent is None:
            QMessageBox.warning(self, "提示", "请先框选解译范围")
            return

        params = self.get_task_parameters()
        if params is None: return

        canvas_crs = self.canvas.mapSettings().destinationCrs()
        self._task_canvas_extent = QgsRectangle(self.selected_extent)
        self._task_canvas_crs = canvas_crs

        self._set_running_state(True)
        self.status_label.setText("正在打包影像并提交任务，请稍候...")
        actual_model_key = self.model_key or get_model_key_by_mode(self.mode)

        task = InterpretTask(
            raster_layer=layer_t1,
            raster_layer_after=params.get("layer_t2"),
            extent=self.selected_extent,
            extent_crs=canvas_crs,
            model_key=actual_model_key,
            target_class=params.get("target_class", ""),
            prompt=params.get("prompt", ""),
            output_format=params.get("output_format", "mask"),
            server_url=server_url,
            machine_id=self.dock.machine_id,
            token=token,
        )
        task.progressMessage.connect(lambda text: self.status_label.setText(text))
        task.taskSucceeded.connect(self._on_finished_ok)
        task.taskFailed.connect(self._on_finished_error)
        task.taskCancelled.connect(self._on_cancelled)

        self.task = task
        self.dock.active_running_task = task
        QgsApplication.taskManager().addTask(task)

    def _cancel_task(self):
        if self.task is not None:
            self.cancel_btn.setEnabled(False)
            self.status_label.setText("正在打断任务，请稍候...")
            self.task.cancel()

    def _set_running_state(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.progress_bar.setVisible(running)

    def _on_finished_ok(self, result_path, content_type):
        self._set_running_state(False)
        self.task = None
        self.dock.active_running_task = None
        time_display = datetime.now().strftime("%H:%M:%S")
        layer_name = f"解译结果_{self.model_key}({time_display})"

        if result_path.endswith('.tif'):
            new_layer = QgsRasterLayer(result_path, layer_name)
        else:
            new_layer = QgsVectorLayer(result_path, layer_name, "ogr")

        if not new_layer.isValid():
            QMessageBox.critical(self, "错误", "解译结果加载失败")
            self.status_label.setText("加载结果失败")
            return

        if hasattr(self, '_task_canvas_extent') and hasattr(self, '_task_canvas_crs'):
            clipped = self._clip_layer_to_extent(
                new_layer, result_path, QgsRectangle(self._task_canvas_extent), self._task_canvas_crs, layer_name
            )
            if clipped and clipped.isValid():
                new_layer = clipped

        QgsProject.instance().addMapLayer(new_layer)
        self.status_label.setText(f"解译完成！已成功加载图层: {layer_name}")
        self.dock.refresh_account_info(silent=True)

    def _on_finished_error(self, error_msg):
        self._set_running_state(False)
        self.task = None
        self.dock.active_running_task = None
        self.status_label.setText("解译失败")
        QMessageBox.critical(self, "解译失败", f"{error_msg}\n\n💡 提示：若遇到网关错误，可在设置页刷新网关。")
        if "登录已过期" in error_msg:
            self.dock.logout()
        elif "免费次数已用完" in error_msg or "402" in error_msg:
            self.dock.open_plan_dialog()

    def _on_cancelled(self):
        self._set_running_state(False)
        self.task = None
        self.dock.active_running_task = None
        self.status_label.setText("任务已成功打断")

    def _clip_layer_to_extent(self, layer, result_path, extent, extent_crs, layer_name):
        try:
            layer_crs = layer.crs()
            if extent_crs != layer_crs:
                transform = QgsCoordinateTransform(extent_crs, layer_crs, QgsProject.instance())
                extent = transform.transformBoundingBox(extent)
        except Exception as e:
            print(f"[clip] 坐标转换异常: {e}")
            return None

        if isinstance(layer, QgsRasterLayer):
            try:
                out_path = os.path.join(tempfile.gettempdir(), f"clipped_{id(self)}.tif")
                proj_win = [extent.xMinimum(), extent.yMaximum(), extent.xMaximum(), extent.yMinimum()]
                ds = gdal.Translate(out_path, result_path, options=gdal.TranslateOptions(projWin=proj_win))
                ds = None
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return QgsRasterLayer(out_path, layer_name)
            except Exception as e:
                print(f"[clip] 栅格裁切失败: {e}")
        else:
            try:
                from qgis import processing
                result = processing.run("native:extractbyextent", {
                    'INPUT': result_path, 'EXTENT': extent, 'OUTPUT': 'memory:'
                })
                clipped = result['OUTPUT']
                if clipped and clipped.isValid():
                    clipped.setName(layer_name)
                    return clipped
            except Exception as e:
                print(f"[clip] 矢量裁切失败: {e}")
        return None


# =========================================================================
# 二、 AI 深度解译专项页面
# =========================================================================
class LanduseMultiTaskWidget(BaseTaskWidget):
    """土地利用全要素多选综合解译"""

    def __init__(self, main_dock, parent=None):
        real_key = get_model_key_by_mode("landuse")
        super().__init__(main_dock, model_key=real_key, mode="landuse", parent=parent)

    def build_parameters_ui(self, layout: QVBoxLayout):
        self.param_group.setTitle("2. 选择解译要素 (多选)")
        class_grid = QGridLayout()
        self.class_checkboxes = {}
        for idx, (label, cls_id) in enumerate(LANDUSE_CLASSES):
            cb = QCheckBox(label)
            if cls_id in DEFAULT_CHECKED_CLASS_IDS: cb.setChecked(True)
            self.class_checkboxes[cls_id] = cb
            class_grid.addWidget(cb, idx // 3, idx % 3)
        layout.addLayout(class_grid)

    def get_task_parameters(self) -> dict:
        selected_ids = [str(cls_id) for cls_id, cb in self.class_checkboxes.items() if cb.isChecked()]
        if not selected_ids:
            QMessageBox.warning(self, "提示", "请至少勾选一个要素类别")
            return None
        return {"target_class": ",".join(selected_ids), "output_format": "mask"}


class SingleThemeExtractionWidget(BaseTaskWidget):
    """单要素专项提取通用组件 (建筑 / 水体 / 林地 / 耕地 / 道路)"""

    def __init__(self, main_dock, target_class_id: str, desc: str, parent=None):
        self.fixed_target_class = target_class_id
        self.desc_text = desc
        real_key = get_model_key_by_mode("landuse")
        super().__init__(main_dock, model_key=real_key, mode="landuse", parent=parent)

    def build_parameters_ui(self, layout: QVBoxLayout):
        self.param_group.setTitle("2. 专项提取说明")
        lbl = QLabel(self.desc_text)
        lbl.setStyleSheet("color: #475569; font-size: 12px; line-height: 140%;")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

    def get_task_parameters(self) -> dict:
        return {"target_class": self.fixed_target_class, "output_format": "mask"}


class Sam3TaskWidget(BaseTaskWidget):
    """SAM3 交互大模型提示词解译"""

    def __init__(self, main_dock, parent=None):
        real_key = get_model_key_by_mode("sam3", fallback_key="sam3")
        super().__init__(main_dock, model_key=real_key, mode="sam3", parent=parent)

    def build_parameters_ui(self, layout: QVBoxLayout):
        self.param_group.setTitle("2. SAM3 提示词配置")
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("例如: road, water, building, solar panel (输入英文提示词)")
        self.prompt_edit.setFixedHeight(50)
        layout.addWidget(self.prompt_edit)

        sam_out_layout = QHBoxLayout()
        sam_out_layout.addWidget(QLabel("输出形式:"))
        self.sam_out_type_combo = QComboBox()
        self.sam_out_type_combo.addItem("矢量分割图斑 (Polygon)", "mask")
        self.sam_out_type_combo.addItem("目标检测方框 (Bounding Box)", "bbox")
        sam_out_layout.addWidget(self.sam_out_type_combo)
        layout.addLayout(sam_out_layout)

    def get_task_parameters(self) -> dict:
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "提示", "请输入提示词 (Prompt)")
            return None
        return {"prompt": prompt, "output_format": self.sam_out_type_combo.currentData() or "mask"}


class ChangeDetectionTaskWidget(BaseTaskWidget):
    """深度双期影像变化检测"""

    def __init__(self, main_dock, parent=None):
        real_key = get_model_key_by_mode("change_detection", fallback_key="change_detection")
        super().__init__(main_dock, model_key=real_key, mode="change_detection", parent=parent)

    def _build_base_ui(self):
        super()._build_base_ui()
        self.lbl_t2 = QLabel("变化期影像 (T2 后期):")
        self.layer_combo_t2 = QgsMapLayerComboBox()
        self.layer_combo_t2.setFilters(RASTER_LAYER_FILTER)
        self.custom_layer_layout.addWidget(self.lbl_t2)
        self.custom_layer_layout.addWidget(self.layer_combo_t2)

    def build_parameters_ui(self, layout: QVBoxLayout):
        self.param_group.setTitle("2. 深度变化检测说明")
        lbl = QLabel("模型将自动对比两期时相的影像特征，输出区域内新增、拆除或变化的斑块图层。")
        lbl.setStyleSheet("color: #64748b; font-size: 12px;")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

    def get_task_parameters(self) -> dict:
        layer_t2 = self.layer_combo_t2.currentLayer()
        if layer_t2 is None or not isinstance(layer_t2, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "必须同时选择后期 T2 栅格图层")
            return None
        return {"layer_t2": layer_t2, "output_format": "mask"}


# =========================================================================
# 三、 10 大类本地免费遥感与 GIS 工具箱 (100% 本地极速 / 0 Token)
# =========================================================================

# 1. 全能遥感光谱指数计算器 (10+ 指数支持)
class SpectralIndexTaskWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self.canvas = main_dock.canvas
        self.extent_tool = None
        self.selected_extent = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入多波段栅格图层")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        self.layer_combo.layerChanged.connect(self._on_layer_changed)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 指数类型与波段配置")
        param_grid = QGridLayout(param_group)

        param_grid.addWidget(QLabel("计算指数:"), 0, 0)
        self.index_combo = QComboBox()
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
        self.spin_b1 = QSpinBox()
        self.spin_b1.setRange(1, 64)
        self.spin_b1.setValue(4)
        param_grid.addWidget(self.lbl_b1, 1, 0)
        param_grid.addWidget(self.spin_b1, 1, 1)

        self.lbl_b2 = QLabel("红光 (Red):")
        self.spin_b2 = QSpinBox()
        self.spin_b2.setRange(1, 64)
        self.spin_b2.setValue(3)
        param_grid.addWidget(self.lbl_b2, 2, 0)
        param_grid.addWidget(self.spin_b2, 2, 1)

        self.lbl_b3 = QLabel("蓝光 (Blue):")
        self.spin_b3 = QSpinBox()
        self.spin_b3.setRange(1, 64)
        self.spin_b3.setValue(1)
        self.lbl_b3.setVisible(False)
        self.spin_b3.setVisible(False)
        param_grid.addWidget(self.lbl_b3, 3, 0)
        param_grid.addWidget(self.spin_b3, 3, 1)

        self.cb_threshold = QCheckBox("启用阈值二值化提取:")
        self.spin_threshold = QDoubleSpinBox()
        self.spin_threshold.setRange(-1.0, 1.0)
        self.spin_threshold.setSingleStep(0.05)
        self.spin_threshold.setValue(0.2)
        self.spin_threshold.setEnabled(False)
        self.cb_threshold.toggled.connect(self.spin_threshold.setEnabled)

        param_grid.addWidget(self.cb_threshold, 4, 0)
        param_grid.addWidget(self.spin_threshold, 4, 1)
        layout.addWidget(param_group)

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
        self.lbl_b3.setVisible(False)
        self.spin_b3.setVisible(False)

        if idx_type in ["ndvi", "fvc"]:
            self.lbl_b1.setText("近红外 (NIR):")
            self.lbl_b2.setText("红光 (Red):")
            self.spin_threshold.setValue(0.2)
        elif idx_type == "gndvi":
            self.lbl_b1.setText("近红外 (NIR):")
            self.lbl_b2.setText("绿光 (Green):")
            self.spin_threshold.setValue(0.2)
        elif idx_type == "savi":
            self.lbl_b1.setText("近红外 (NIR):")
            self.lbl_b2.setText("红光 (Red):")
            self.spin_threshold.setValue(0.15)
        elif idx_type in ["evi", "bsi"]:
            self.lbl_b1.setText("近红外 (NIR):" if idx_type == "evi" else "短波红外 (SWIR):")
            self.lbl_b2.setText("红光 (Red):")
            self.lbl_b3.setText("蓝光 (Blue):" if idx_type == "evi" else "近红外 (NIR):")
            self.lbl_b3.setVisible(True)
            self.spin_b3.setVisible(True)
            self.spin_threshold.setValue(0.2)
        elif idx_type == "ndwi":
            self.lbl_b1.setText("绿光 (Green):")
            self.lbl_b2.setText("近红外 (NIR):")
            self.spin_threshold.setValue(0.0)
        elif idx_type == "mndwi":
            self.lbl_b1.setText("绿光 (Green):")
            self.lbl_b2.setText("短波红外 (SWIR):")
            self.spin_threshold.setValue(0.0)
        elif idx_type == "ndbi":
            self.lbl_b1.setText("短波红外 (SWIR):")
            self.lbl_b2.setText("近红外 (NIR):")
            self.spin_threshold.setValue(0.0)
        elif idx_type in ["ndmi", "nbr"]:
            self.lbl_b1.setText("近红外 (NIR):")
            self.lbl_b2.setText("短波红外 (SWIR):")
            self.spin_threshold.setValue(0.1)

    def _on_layer_changed(self):
        layer = self.layer_combo.currentLayer()
        if layer and isinstance(layer, QgsRasterLayer):
            band_count = layer.bandCount()
            for sp in [self.spin_b1, self.spin_b2, self.spin_b3]:
                sp.setMaximum(band_count)
            if band_count >= 4:
                self.spin_b1.setValue(4);
                self.spin_b2.setValue(3);
                self.spin_b3.setValue(1)
            elif band_count >= 2:
                self.spin_b1.setValue(2);
                self.spin_b2.setValue(1)

    def _activate_extent_tool(self):
        self.extent_tool = ExtentSelectTool(self.canvas)
        self.extent_tool.extentSelected.connect(self._on_extent_selected)
        self.canvas.setMapTool(self.extent_tool)
        self.status_label.setText("请在地图上按住左键拖拽框选范围")

    def _use_canvas_extent(self):
        self._on_extent_selected(self.canvas.extent())

    def _on_extent_selected(self, rect: QgsRectangle):
        self.selected_extent = rect
        crs_id = self.canvas.mapSettings().destinationCrs().authid()
        self.extent_label.setText(
            f"X: [{rect.xMinimum():.2f}, {rect.xMaximum():.2f}]\n"
            f"Y: [{rect.yMinimum():.2f}, {rect.yMaximum():.2f}]\n"
            f"CRS: {crs_id}"
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

        b1_idx = self.spin_b1.value()
        b2_idx = self.spin_b2.value()
        b3_idx = self.spin_b3.value()
        idx_type = self.index_combo.currentData()
        idx_name = self.index_combo.currentText().split(' ')[0]

        self.status_label.setText("正在计算栅格指数...")
        QApplication.processEvents()

        try:
            temp_crop_path = os.path.join(tempfile.gettempdir(), f"src_{id(self)}.tif")
            if self.selected_extent:
                canvas_crs = self.canvas.mapSettings().destinationCrs()
                layer_crs = layer.crs()
                ext = self.selected_extent
                if canvas_crs != layer_crs:
                    tr = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
                    ext = tr.transformBoundingBox(ext)
                proj_win = [ext.xMinimum(), ext.yMaximum(), ext.xMaximum(), ext.yMinimum()]
                ds_crop = gdal.Translate(temp_crop_path, source_path, projWin=proj_win)
                ds_crop = None
                read_path = temp_crop_path
            else:
                read_path = source_path

            ds = gdal.Open(read_path)
            b1 = ds.GetRasterBand(b1_idx).ReadAsArray().astype(np.float32)
            b2 = ds.GetRasterBand(b2_idx).ReadAsArray().astype(np.float32)

            if idx_type == "savi":
                L = 0.5;
                denom = b1 + b2 + L;
                denom[denom == 0] = np.nan
                index_arr = ((b1 - b2) / denom) * (1.0 + L)
            elif idx_type == "evi":
                b3 = ds.GetRasterBand(b3_idx).ReadAsArray().astype(np.float32)
                denom = b1 + 6.0 * b2 - 7.5 * b3 + 1.0;
                denom[denom == 0] = np.nan
                index_arr = 2.5 * (b1 - b2) / denom
            elif idx_type == "fvc":
                denom = b1 + b2;
                denom[denom == 0] = np.nan
                ndvi = (b1 - b2) / denom
                index_arr = np.clip((ndvi - 0.05) / (0.70 - 0.05 + 1e-6), 0.0, 1.0)
            elif idx_type == "bsi":
                b3 = ds.GetRasterBand(b3_idx).ReadAsArray().astype(np.float32)
                num = (b1 + b2) - (b3 + 0)
                den = (b1 + b2) + (b3 + 0)
                den[den == 0] = np.nan
                index_arr = num / den
            else:
                denom = b1 + b2;
                denom[denom == 0] = np.nan
                index_arr = (b1 - b2) / denom

            if self.cb_threshold.isChecked():
                threshold = self.spin_threshold.value()
                out_arr = np.where(index_arr >= threshold, 1, 0).astype(np.uint8)
                out_dtype = gdal.GDT_Byte;
                is_binary = True
            else:
                out_arr = index_arr;
                out_dtype = gdal.GDT_Float32;
                is_binary = False

            time_str = datetime.now().strftime("%H%M%S")
            out_file = os.path.join(tempfile.gettempdir(), f"{idx_name}_{time_str}.tif")
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(out_file, ds.RasterXSize, ds.RasterYSize, 1, out_dtype)
            out_ds.SetGeoTransform(ds.GetGeoTransform())
            out_ds.SetProjection(ds.GetProjection())
            out_band = out_ds.GetRasterBand(1)
            if not is_binary:
                out_band.SetNoDataValue(-9999)
                out_arr = np.nan_to_num(out_arr, nan=-9999)
            out_band.WriteArray(out_arr)
            out_ds = None;
            ds = None

            result_layer_name = f"{idx_name}_计算结果({datetime.now().strftime('%H:%M:%S')})"
            res_layer = QgsRasterLayer(out_file, result_layer_name)
            if res_layer.isValid():
                QgsProject.instance().addMapLayer(res_layer)
                self.status_label.setText(f"计算完成！已加载图层: {result_layer_name}")
                QMessageBox.information(self, "成功", f"指数计算完成，图层已加载！")
        except Exception as e:
            self.status_label.setText("计算出错")
            QMessageBox.critical(self, "错误", f"指数计算失败: {e}")


# 2. PCA 主成分分析 (多波段正交变换与降维)
class PcaTransformWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入多波段栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. PCA 变换设置")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("输出主成分数量:"), 0, 0)
        self.spin_comp = QSpinBox()
        self.spin_comp.setRange(1, 6)
        self.spin_comp.setValue(3)
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
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_pca(self):
        layer = self.layer_combo.currentLayer()
        if not layer or layer.bandCount() < 2:
            QMessageBox.warning(self, "提示", "请选择至少包含 2 个波段的栅格图层")
            return

        n_comp = self.spin_comp.value()
        self.status_label.setText("正在执行主成分正交变换...")
        QApplication.processEvents()

        try:
            ds = gdal.Open(layer.source())
            bands = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
            h, w = bands[0].shape
            X = np.stack(bands, axis=-1).reshape(-1, len(bands))

            mean = np.mean(X, axis=0)
            X_centered = X - mean

            u, s, vt = np.linalg.svd(X_centered, full_matrices=False)
            pcs = np.dot(X_centered, vt.T[:, :n_comp])

            time_str = datetime.now().strftime("%H%M%S")
            out_file = os.path.join(tempfile.gettempdir(), f"PCA_{n_comp}B_{time_str}.tif")
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(out_file, w, h, n_comp, gdal.GDT_Float32)
            out_ds.SetGeoTransform(ds.GetGeoTransform())
            out_ds.SetProjection(ds.GetProjection())

            for i in range(n_comp):
                pc_band = pcs[:, i].reshape(h, w)
                out_ds.GetRasterBand(i + 1).WriteArray(pc_band)

            out_ds = None;
            ds = None
            res_layer = QgsRasterLayer(out_file, f"PCA主成分_{n_comp}B({time_str})")
            if res_layer.isValid():
                QgsProject.instance().addMapLayer(res_layer)
                self.status_label.setText(f"PCA 变换完成！已输出 {n_comp} 个主成分图层")
                QMessageBox.information(self, "成功", f"成功生成 {n_comp} 个主成分特征波段！")
        except Exception as e:
            self.status_label.setText("PCA 计算失败")
            QMessageBox.critical(self, "错误", f"PCA 变换失败: {e}")


# 3. DEM 地形全要素分析 (坡度/坡向/阴影/起伏度)
class DemAnalysisWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

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
        self.spin_z = QDoubleSpinBox()
        self.spin_z.setRange(0.1, 100.0)
        self.spin_z.setValue(1.0)
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
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_dem(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择 DEM 栅格图层")
            return

        dem_type = self.dem_type_combo.currentData()
        name_label = self.dem_type_combo.currentText().split(' ')[0]
        z_factor = self.spin_z.value()

        self.status_label.setText("正在计算地形特征...")
        QApplication.processEvents()

        try:
            time_str = datetime.now().strftime("%H%M%S")
            out_file = os.path.join(tempfile.gettempdir(), f"{dem_type}_{time_str}.tif")
            options = gdal.DEMProcessingOptions(zFactor=z_factor)
            ds = gdal.DEMProcessing(out_file, layer.source(), dem_type, options=options)
            ds = None

            res_layer = QgsRasterLayer(out_file, f"{name_label}_{time_str}")
            if res_layer.isValid():
                QgsProject.instance().addMapLayer(res_layer)
                self.status_label.setText("地形分析完成！图层已加载")
                QMessageBox.information(self, "成功", f"地形分析 [{name_label}] 完成！")
        except Exception as e:
            self.status_label.setText("地形分析失败")
            QMessageBox.critical(self, "错误", f"DEM 分析失败: {e}")


# 4. 空间滤波与边缘轮廓检测 (Sobel / 高斯 / 锐化)
class SpatialFilterWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 滤波与算子设置")
        param_grid = QGridLayout(param_group)

        param_grid.addWidget(QLabel("选择算子:"), 0, 0)
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("🔎 Sobel 边缘梯度提取 (道路/地界识别)", "sobel")
        self.filter_combo.addItem("🫧 高斯平滑降噪 (Gaussian Blur)", "gaussian")
        self.filter_combo.addItem("🪄 拉普拉斯高通锐化 (Sharpening)", "laplacian")
        param_grid.addWidget(self.filter_combo, 0, 1)

        param_grid.addWidget(QLabel("波段序号:"), 1, 0)
        self.spin_band = QSpinBox()
        self.spin_band.setRange(1, 64)
        self.spin_band.setValue(1)
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
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_filter(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择栅格图层")
            return

        f_type = self.filter_combo.currentData()
        b_idx = self.spin_band.value()

        self.status_label.setText("正在执行空间滤波卷积...")
        QApplication.processEvents()

        try:
            ds = gdal.Open(layer.source())
            arr = ds.GetRasterBand(b_idx).ReadAsArray().astype(np.float32)

            if f_type == "sobel":
                kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
                ky = np.array([[1, 2, 1], [0, 0, 0], [-1, -2, -1]], dtype=np.float32)
                gx = np.zeros_like(arr);
                gy = np.zeros_like(arr)
                for r in range(1, arr.shape[0] - 1):
                    for c in range(1, arr.shape[1] - 1):
                        sub = arr[r - 1:r + 2, c - 1:c + 2]
                        gx[r, c] = np.sum(sub * kx)
                        gy[r, c] = np.sum(sub * ky)
                out_arr = np.sqrt(gx ** 2 + gy ** 2)
            elif f_type == "gaussian":
                k = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], dtype=np.float32) / 16.0
                out_arr = np.zeros_like(arr)
                for r in range(1, arr.shape[0] - 1):
                    for c in range(1, arr.shape[1] - 1):
                        out_arr[r, c] = np.sum(arr[r - 1:r + 2, c - 1:c + 2] * k)
            else:
                k = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]], dtype=np.float32)
                out_arr = np.zeros_like(arr)
                for r in range(1, arr.shape[0] - 1):
                    for c in range(1, arr.shape[1] - 1):
                        out_arr[r, c] = np.sum(arr[r - 1:r + 2, c - 1:c + 2] * k)

            time_str = datetime.now().strftime("%H%M%S")
            out_file = os.path.join(tempfile.gettempdir(), f"filter_{f_type}_{time_str}.tif")
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(out_file, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Float32)
            out_ds.SetGeoTransform(ds.GetGeoTransform())
            out_ds.SetProjection(ds.GetProjection())
            out_ds.GetRasterBand(1).WriteArray(out_arr)
            out_ds = None;
            ds = None

            res_layer = QgsRasterLayer(out_file, f"滤波_{f_type}_{time_str}")
            if res_layer.isValid():
                QgsProject.instance().addMapLayer(res_layer)
                self.status_label.setText("滤波处理完成！图层已加载")
                QMessageBox.information(self, "成功", "空间滤波与边缘提取完成！")
        except Exception as e:
            self.status_label.setText("滤波失败")
            QMessageBox.critical(self, "错误", f"计算失败: {e}")


# 5. 地物分类面积统计与报表工具
class AreaStatisticsWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入分类/解译栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
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

        try:
            ds = gdal.Open(layer.source())
            arr = ds.GetRasterBand(1).ReadAsArray()
            gt = ds.GetGeoTransform()
            pixel_area = abs(gt[1] * gt[5])

            unique, counts = np.unique(arr[arr > 0], return_counts=True)
            total_pixels = np.sum(counts)

            self.table.setRowCount(len(unique))
            for i, (val, count) in enumerate(zip(unique, counts)):
                area_m2 = count * pixel_area
                area_mu = area_m2 / 666.6667
                percent = (count / total_pixels) * 100.0

                self.table.setItem(i, 0, QTableWidgetItem(f"类别 {val}"))
                self.table.setItem(i, 1, QTableWidgetItem(f"{count:,}"))
                self.table.setItem(i, 2, QTableWidgetItem(f"{area_m2:,.2f}"))
                self.table.setItem(i, 3, QTableWidgetItem(f"{area_mu:,.2f}"))
                self.table.setItem(i, 4, QTableWidgetItem(f"{percent:.2f}%"))
            ds = None
            QMessageBox.information(self, "统计完成", f"成功统计 {len(unique)} 种地物要素的面积分布！")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"统计面积异常: {e}")


# 6. 矢量图斑化简与边界平滑
class VectorSmoothSimplifyWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入待处理矢量图层")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(VECTOR_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 几何精修参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("化简容差距离 (米):"), 0, 0)
        self.spin_tol = QDoubleSpinBox();
        self.spin_tol.setRange(0.01, 100.0);
        self.spin_tol.setValue(1.0)
        param_grid.addWidget(self.spin_tol, 0, 1)

        param_grid.addWidget(QLabel("平滑迭代次数:"), 1, 0)
        self.spin_iter = QSpinBox();
        self.spin_iter.setRange(1, 10);
        self.spin_iter.setValue(2)
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
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_simplify(self):
        layer = self.layer_combo.currentLayer()
        if not layer or not isinstance(layer, QgsVectorLayer):
            QMessageBox.warning(self, "提示", "请选择有效的矢量图层")
            return

        tol = self.spin_tol.value()
        iters = self.spin_iter.value()
        self.status_label.setText("正在执行几何化简与平滑...")
        QApplication.processEvents()

        try:
            from qgis import processing
            res_simp = processing.run("native:simplifygeometries", {
                'INPUT': layer, 'METHOD': 0, 'TOLERANCE': tol, 'OUTPUT': 'memory:'
            })
            res_smooth = processing.run("native:smoothgeometry", {
                'INPUT': res_simp['OUTPUT'], 'ITERATIONS': iters, 'OFFSET': 0.25, 'OUTPUT': 'memory:'
            })
            out_layer = res_smooth['OUTPUT']
            time_str = datetime.now().strftime("%H:%M:%S")
            out_layer.setName(f"{layer.name()}_精修平滑({time_str})")
            QgsProject.instance().addMapLayer(out_layer)
            self.status_label.setText("几何精修完成！已加载图层")
            QMessageBox.information(self, "成功", "图斑化简与平滑处理完成！")
        except Exception as e:
            self.status_label.setText("处理失败")
            QMessageBox.critical(self, "错误", f"矢量精修异常: {e}")


# 7. K-Means 智能无监督聚类
class KMeansClusterWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. K-Means 聚类参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("地物聚类类别数 (K):"), 0, 0)
        self.spin_k = QSpinBox();
        self.spin_k.setRange(2, 15);
        self.spin_k.setValue(5)
        param_grid.addWidget(self.spin_k, 0, 1)

        param_grid.addWidget(QLabel("最大迭代次数:"), 1, 0)
        self.spin_iter = QSpinBox();
        self.spin_iter.setRange(5, 50);
        self.spin_iter.setValue(15)
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
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_kmeans(self):
        layer = self.layer_combo.currentLayer()
        if not layer or not isinstance(layer, QgsRasterLayer):
            QMessageBox.warning(self, "提示", "请选择有效的栅格图层")
            return

        k = self.spin_k.value()
        max_iters = self.spin_iter.value()
        self.status_label.setText("正在执行像元聚类中...")
        QApplication.processEvents()

        try:
            ds = gdal.Open(layer.source())
            bands = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.float32) for i in range(ds.RasterCount)]
            h, w = bands[0].shape
            X = np.stack(bands, axis=-1).reshape(-1, len(bands))

            np.random.seed(42)
            indices = np.random.choice(X.shape[0], k, replace=False)
            centers = X[indices]

            for _ in range(max_iters):
                dists = np.linalg.norm(X[:, None, :] - centers[None, :, :], axis=-1)
                labels = np.argmin(dists, axis=-1)
                new_centers = np.array(
                    [X[labels == j].mean(axis=0) if np.any(labels == j) else centers[j] for j in range(k)])
                if np.allclose(centers, new_centers, atol=1e-2): break
                centers = new_centers

            cluster_map = labels.reshape(h, w).astype(np.uint8) + 1
            out_file = os.path.join(tempfile.gettempdir(), f"KMeans_K{k}_{datetime.now().strftime('%H%M%S')}.tif")
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(out_file, w, h, 1, gdal.GDT_Byte)
            out_ds.SetGeoTransform(ds.GetGeoTransform())
            out_ds.SetProjection(ds.GetProjection())
            out_ds.GetRasterBand(1).WriteArray(cluster_map)
            out_ds = None;
            ds = None

            res_layer = QgsRasterLayer(out_file, f"KMeans聚类(K={k})")
            if res_layer.isValid():
                QgsProject.instance().addMapLayer(res_layer)
                self.status_label.setText(f"聚类完成！共生成 {k} 个地物类别")
                QMessageBox.information(self, "成功", f"K-Means 聚类成功！")
        except Exception as e:
            self.status_label.setText("聚类失败")
            QMessageBox.critical(self, "错误", f"K-Means 执行异常: {e}")


# 8. 双期像元差分变化检测
class RasterDiffChangeWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入双期影像")
        layer_v = QVBoxLayout(layer_group)
        layer_v.addWidget(QLabel("基准期 (T1 前期):"))
        self.combo_t1 = QgsMapLayerComboBox();
        self.combo_t1.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.combo_t1)
        layer_v.addWidget(QLabel("变化期 (T2 后期):"))
        self.combo_t2 = QgsMapLayerComboBox();
        self.combo_t2.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.combo_t2)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 差分参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("差分波段:"), 0, 0)
        self.spin_band = QSpinBox();
        self.spin_band.setRange(1, 64);
        self.spin_band.setValue(1)
        param_grid.addWidget(self.spin_band, 0, 1)

        param_grid.addWidget(QLabel("变化灵敏度阈值:"), 1, 0)
        self.spin_thresh = QDoubleSpinBox();
        self.spin_thresh.setRange(1.0, 500.0);
        self.spin_thresh.setValue(30.0)
        param_grid.addWidget(self.spin_thresh, 1, 1)

        self.cb_polygonize = QCheckBox("同时输出矢量斑块图层 (Shape)")
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
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_diff(self):
        l1 = self.combo_t1.currentLayer();
        l2 = self.combo_t2.currentLayer()
        if not l1 or not l2:
            QMessageBox.warning(self, "提示", "请选择两期输入图层")
            return

        b_idx = self.spin_band.value()
        thresh = self.spin_thresh.value()
        self.status_label.setText("正在计算像元差分...")
        QApplication.processEvents()

        try:
            ds1 = gdal.Open(l1.source());
            ds2 = gdal.Open(l2.source())
            arr1 = ds1.GetRasterBand(b_idx).ReadAsArray().astype(np.float32)
            arr2 = ds2.GetRasterBand(b_idx).ReadAsArray().astype(np.float32)
            if arr1.shape != arr2.shape: raise Exception("两期分辨率或行列数不匹配")

            diff = np.abs(arr2 - arr1)
            change_mask = np.where(diff >= thresh, 1, 0).astype(np.uint8)

            time_str = datetime.now().strftime("%H%M%S")
            out_tif = os.path.join(tempfile.gettempdir(), f"diff_{time_str}.tif")
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(out_tif, ds1.RasterXSize, ds1.RasterYSize, 1, gdal.GDT_Byte)
            out_ds.SetGeoTransform(ds1.GetGeoTransform())
            out_ds.SetProjection(ds1.GetProjection())
            band_out = out_ds.GetRasterBand(1)
            band_out.WriteArray(change_mask)
            band_out.FlushCache()

            if self.cb_polygonize.isChecked():
                shp_driver = ogr.GetDriverByName("ESRI Shapefile")
                out_shp = os.path.join(tempfile.gettempdir(), f"diff_poly_{time_str}.shp")
                srs = osr.SpatialReference()
                srs.ImportFromWkt(ds1.GetProjection())
                shp_ds = shp_driver.CreateDataSource(out_shp)
                shp_layer = shp_ds.CreateLayer("change", srs, ogr.wkbPolygon)
                shp_layer.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))
                gdal.Polygonize(band_out, band_out, shp_layer, 0, [], callback=None)
                shp_ds = None
                vec_layer = QgsVectorLayer(out_shp, f"像元差分变化斑块({time_str})", "ogr")
                if vec_layer.isValid(): QgsProject.instance().addMapLayer(vec_layer)

            out_ds = None;
            ds1 = None;
            ds2 = None
            diff_layer = QgsRasterLayer(out_tif, f"像元变化掩膜({time_str})")
            if diff_layer.isValid():
                QgsProject.instance().addMapLayer(diff_layer)
                self.status_label.setText("差分变化检测完成！")
                QMessageBox.information(self, "成功", "双期像元差分检测完成！")
        except Exception as e:
            self.status_label.setText("差分检测失败")
            QMessageBox.critical(self, "错误", f"计算失败: {e}")


# 9. 假彩色合成与画质增强
class ImageEnhanceWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入多波段栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. RGB 通道波段分配")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("红通道 (R):"), 0, 0)
        self.spin_r = QSpinBox();
        self.spin_r.setRange(1, 64);
        self.spin_r.setValue(4)
        param_grid.addWidget(self.spin_r, 0, 1)

        param_grid.addWidget(QLabel("绿通道 (G):"), 1, 0)
        self.spin_g = QSpinBox();
        self.spin_g.setRange(1, 64);
        self.spin_g.setValue(3)
        param_grid.addWidget(self.spin_g, 1, 1)

        param_grid.addWidget(QLabel("蓝通道 (B):"), 2, 0)
        self.spin_b = QSpinBox();
        self.spin_b.setRange(1, 64);
        self.spin_b.setValue(2)
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
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_enhance(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择栅格图层")
            return

        r_idx = self.spin_r.value();
        g_idx = self.spin_g.value();
        b_idx = self.spin_b.value()
        self.status_label.setText("正在合成与拉伸波段...")
        QApplication.processEvents()

        try:
            ds = gdal.Open(layer.source())
            bands_data = []
            for b_idx in [r_idx, g_idx, b_idx]:
                arr = ds.GetRasterBand(b_idx).ReadAsArray().astype(np.float32)
                if self.cb_stretch.isChecked():
                    p2, p98 = np.percentile(arr[arr > 0], (2, 98))
                    arr = np.clip((arr - p2) / (p98 - p2 + 1e-5) * 255.0, 0, 255)
                bands_data.append(arr.astype(np.uint8))

            time_str = datetime.now().strftime("%H%M%S")
            out_file = os.path.join(tempfile.gettempdir(), f"composite_{time_str}.tif")
            driver = gdal.GetDriverByName("GTiff")
            out_ds = driver.Create(out_file, ds.RasterXSize, ds.RasterYSize, 3, gdal.GDT_Byte)
            out_ds.SetGeoTransform(ds.GetGeoTransform())
            out_ds.SetProjection(ds.GetProjection())
            for i, b_arr in enumerate(bands_data):
                out_ds.GetRasterBand(i + 1).WriteArray(b_arr)
            out_ds = None;
            ds = None

            res_layer = QgsRasterLayer(out_file, f"增强假彩色({time_str})")
            if res_layer.isValid():
                QgsProject.instance().addMapLayer(res_layer)
                self.status_label.setText("生成成功！已加载增强影像")
                QMessageBox.information(self, "成功", "假彩色增强影像生成完成！")
        except Exception as e:
            self.status_label.setText("生成失败")
            QMessageBox.critical(self, "错误", f"合成失败: {e}")


# 10. 栅格转矢量与噪点过滤
class RasterPolygonizeWidget(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        layer_group = QGroupBox("1. 输入单波段/分类栅格")
        layer_v = QVBoxLayout(layer_group)
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(RASTER_LAYER_FILTER)
        layer_v.addWidget(self.layer_combo)
        layout.addWidget(layer_group)

        param_group = QGroupBox("2. 过滤与转换参数")
        param_grid = QGridLayout(param_group)
        param_grid.addWidget(QLabel("过滤孤立碎斑阈值 (像元数):"), 0, 0)
        self.spin_sieve = QSpinBox();
        self.spin_sieve.setRange(0, 500);
        self.spin_sieve.setValue(4)
        param_grid.addWidget(self.spin_sieve, 0, 1)
        layout.addWidget(param_group)

        run_group = QGroupBox("3. 执行矢量化")
        run_layout = QVBoxLayout(run_group)
        self.run_btn = QPushButton("✨ 一键提取矢量图斑 (0额度)")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self._run_polygonize)
        run_layout.addWidget(self.run_btn)

        self.status_label = QLabel("🧩 将栅格掩膜转为矢量 Polygon 面图层并滤除孤立碎点")
        self.status_label.setStyleSheet("color: #64748b; font-size: 12px;")
        run_layout.addWidget(self.status_label)
        layout.addWidget(run_group)
        layout.addStretch()

    def _run_polygonize(self):
        layer = self.layer_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "提示", "请选择栅格图层")
            return

        sieve_size = self.spin_sieve.value()
        self.status_label.setText("正在提取矢量多边形...")
        QApplication.processEvents()

        try:
            ds = gdal.Open(layer.source())
            src_band = ds.GetRasterBand(1)
            if sieve_size > 0: gdal.SieveFilter(src_band, None, src_band, sieve_size, 4)

            time_str = datetime.now().strftime("%H%M%S")
            out_shp = os.path.join(tempfile.gettempdir(), f"poly_vector_{time_str}.shp")
            driver = ogr.GetDriverByName("ESRI Shapefile")
            srs = osr.SpatialReference()
            srs.ImportFromWkt(ds.GetProjection())
            shp_ds = driver.CreateDataSource(out_shp)
            shp_layer = shp_ds.CreateLayer("polygonized", srs, ogr.wkbPolygon)
            shp_layer.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))

            gdal.Polygonize(src_band, src_band, shp_layer, 0, [], callback=None)
            shp_ds = None;
            ds = None

            vlayer = QgsVectorLayer(out_shp, f"矢量多边形图斑({time_str})", "ogr")
            if vlayer.isValid():
                QgsProject.instance().addMapLayer(vlayer)
                self.status_label.setText("矢量化成功！已添加到图层列表")
                QMessageBox.information(self, "成功", "栅格已转为矢量 Polygon 图斑！")
        except Exception as e:
            self.status_label.setText("矢量化失败")
            QMessageBox.critical(self, "错误", f"转换失败: {e}")


# =========================================================================
# 四、 账号与服务器网关设置页
# =========================================================================
class AccountSettingsPage(QWidget):
    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        acc_group = QGroupBox("用户信息")
        acc_layout = QVBoxLayout(acc_group)

        self.account_status_label = QLabel("尚未登录")
        self.account_status_label.setStyleSheet("color: #64748b; font-size: 13px;")
        acc_layout.addWidget(self.account_status_label)

        self.quota_label = QLabel("")
        acc_layout.addWidget(self.quota_label)

        btn_row = QHBoxLayout()
        self.login_btn = QPushButton("登录 / 注册")
        self.login_btn.clicked.connect(self.dock.open_login_dialog)
        btn_row.addWidget(self.login_btn)

        self.logout_btn = QPushButton("退出登录")
        self.logout_btn.clicked.connect(self.dock.logout)
        self.logout_btn.setVisible(False)
        btn_row.addWidget(self.logout_btn)

        self.upgrade_btn = QPushButton("套餐与充值")
        self.upgrade_btn.clicked.connect(self.dock.open_plan_dialog)
        btn_row.addWidget(self.upgrade_btn)
        acc_layout.addLayout(btn_row)
        layout.addWidget(acc_group)

        server_group = QGroupBox("网络与网关设置")
        server_layout = QVBoxLayout(server_group)

        notice_banner = QLabel("🌸 提示：若遇到连接超时或解译报错，可点击【🔄 刷新网关】同步最新通道。")
        notice_banner.setObjectName("noticeBanner")
        notice_banner.setWordWrap(True)
        server_layout.addWidget(notice_banner)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("网关:"))
        self.server_url_edit = QLineEdit()
        self.server_url_edit.setPlaceholderText(DEFAULT_SERVER_URL or "http://127.0.0.1:8000")
        url_row.addWidget(self.server_url_edit)
        server_layout.addLayout(url_row)

        action_row = QHBoxLayout()
        self.refresh_url_btn = QPushButton("🔄 刷新网关")
        self.refresh_url_btn.clicked.connect(self._refresh_url)
        action_row.addWidget(self.refresh_url_btn)

        self.save_url_btn = QPushButton("💾 保存配置")
        self.save_url_btn.clicked.connect(self._save_custom_url)
        action_row.addWidget(self.save_url_btn)
        server_layout.addLayout(action_row)

        layout.addWidget(server_group)
        layout.addStretch()

    def update_account_ui(self, token, username, account_info):
        if not token:
            self.account_status_label.setText("尚未登录，AI 大模型解译需登录后使用 (本地工具免费无限制)")
            self.account_status_label.setStyleSheet("color: #64748b;")
            self.quota_label.setText("")
            self.login_btn.setVisible(True)
            self.logout_btn.setVisible(False)
            return

        self.login_btn.setVisible(False)
        self.logout_btn.setVisible(True)
        plan = account_info.get("plan", "free")
        plan_label = PLAN_LABELS.get(plan, plan)
        self.account_status_label.setText(f"已登录: <b>{username}</b><br>当前套餐: <b>{plan_label}</b>")
        self.account_status_label.setStyleSheet("color: #15803d; font-size: 13px;")

        if plan == "free":
            used = account_info.get("quota_used_today", 0)
            limit = account_info.get("quota_limit_today") or FREE_PLAN_DAILY_QUOTA
            self.quota_label.setText(f"今日剩余 AI 额度: <b>{max(limit - used, 0)}</b> / {limit} 次")
        elif plan == "pro":
            expire = account_info.get("pro_expire_at", "未知")
            self.quota_label.setText(f"包月会员生效中 (到期: {expire})")
        else:
            self.quota_label.setText("定制版本，无限制")

    def _refresh_url(self):
        self.refresh_url_btn.setEnabled(False)
        self.refresh_url_btn.setText("刷新中...")
        QApplication.processEvents()
        try:
            new_url = fetch_remote_server_url()
            if new_url:
                self.server_url_edit.setText(new_url)
                self.dock.settings.remove(SETTINGS_KEY_SERVER_URL)
                self.dock.settings.setValue("is_custom_server", False)
                QMessageBox.information(self, "成功", f"成功获取最新网关：\n{new_url}")
            else:
                QMessageBox.warning(self, "提示", "未能获取最新地址，请检查网络")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"刷新失败: {e}")
        finally:
            self.refresh_url_btn.setEnabled(True)
            self.refresh_url_btn.setText("🔄 刷新网关")

    def _save_custom_url(self):
        url = self.server_url_edit.text().strip()
        if url:
            self.dock.settings.setValue(SETTINGS_KEY_SERVER_URL, url)
            self.dock.settings.setValue("is_custom_server", True)
            QMessageBox.information(self, "成功", "网关服务器地址已保存！")


# =========================================================================
# 五、 首页卡片式功能大厅 (HomePage)
# =========================================================================
class TaskCardButton(QPushButton):
    def __init__(self, icon_str: str, title: str, subtitle: str, is_free: bool = False, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QPushButton {
                background-color: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 10px;
                text-align: left;
            }
            QPushButton:hover {
                border-color: #3b82f6;
                background-color: #f8fafc;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(10)

        lbl_icon = QLabel(icon_str)
        lbl_icon.setStyleSheet("font-size: 22px; border: none; background: transparent;")
        layout.addWidget(lbl_icon)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)

        title_row = QHBoxLayout()
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #1e293b; border: none; background: transparent;")
        title_row.addWidget(lbl_title)

        if is_free:
            lbl_badge = QLabel("免费")
            lbl_badge.setStyleSheet(
                "font-size: 10px; color: #16a34a; background-color: #dcfce7; border-radius: 4px; padding: 1px 4px; font-weight: bold;")
            title_row.addWidget(lbl_badge)
        title_row.addStretch()
        text_layout.addLayout(title_row)

        lbl_sub = QLabel(subtitle)
        lbl_sub.setStyleSheet("font-size: 11px; color: #64748b; border: none; background: transparent;")
        text_layout.addWidget(lbl_sub)

        layout.addLayout(text_layout)
        layout.addStretch()

        arrow = QLabel("›")
        arrow.setStyleSheet(
            "font-size: 18px; color: #94a3b8; font-weight: bold; border: none; background: transparent;")
        layout.addWidget(arrow)


class HomePage(QWidget):
    taskSelected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # ---------------- 分组 1: 本地免费遥感全能工具箱 ----------------
        lbl_group_free = QLabel("🎈 本地免费遥感全能工具箱 (0额度 / 免联网 / 极速)")
        lbl_group_free.setStyleSheet("font-weight: bold; color: #047857; font-size: 13px; margin-top: 4px;")
        layout.addWidget(lbl_group_free)

        free_cards = [
            ("🍀", "全能遥感光谱指数库", "NDVI / GNDVI / EVI / SAVI / FVC / NDWI / BSI 等10+指数", True, "task_spectral_index"),
            ("🔮", "遥感 PCA 主成分分析", "多波段正交变换，去除冗余生成 PC1/PC2/PC3", True, "task_pca"),
            ("🗻", "DEM 地形全要素分析", "山体阴影(Hillshade) / 坡度 / 坡向 / 起伏度(TRI)", True, "task_dem"),
            ("🔎", "空间滤波与边缘提取", "Sobel 道路/建筑边界提取 + 高斯降噪 + 高通锐化", True, "task_filter"),
            ("🍰", "地物分类面积统计报表", "一键统计分类各类别像元总数、面积与亩数", True, "task_area"),
            ("🎀", "矢量图斑化简与平滑", "Douglas-Peucker 边界精修，消除锯齿边", True, "task_vector_smooth"),
            ("🍭", "K-Means 智能无监督聚类", "多波段自动聚类，快速划分地物类别图斑", True, "task_kmeans"),
            ("🐣", "双期像元差分变化检测", "两期影像绝对差分比对 + 自动矢量化提取", True, "task_raster_diff"),
            ("🌈", "假彩色合成与画质增强", "波段假彩色合成 + 2% 动态对比度拉伸", True, "task_enhance"),
            ("🧩", "栅格一键矢量化与过滤", "二值掩膜直接生成矢量图斑 + 椒盐碎斑过滤", True, "task_polygonize"),
        ]

        for icon, title, desc, is_free, key in free_cards:
            btn = TaskCardButton(icon, title, desc, is_free=is_free)
            btn.clicked.connect(lambda checked=False, k=key: self.taskSelected.emit(k))
            layout.addWidget(btn)

        # ---------------- 分组 2: AI 深度解译大模型 ----------------
        lbl_group_ai = QLabel("🧠 AI 深度学习专项解译大模型")
        lbl_group_ai.setStyleSheet("font-weight: bold; color: #1e3a8a; font-size: 13px; margin-top: 10px;")
        layout.addWidget(lbl_group_ai)

        ai_cards = [
            ("🌻", "土地利用全要素综合解译", "全要素语义分割大模型", False, "task_landuse_multi"),
            ("🏡", "建筑物专项提取", "城镇与乡村建筑轮廓高精度提取", False, "task_building"),
            ("🚗", "道路交通专项提取", "公路、街道与主干交通网络智能提取", False, "task_road"),
            ("🐬", "水系水体专项提取", "河流、湖泊、水库及池塘边界提取", False, "task_water"),
            ("🍄", "林草植被专项提取", "林地、灌木与草地覆盖提取", False, "task_vegetation"),
            ("🥕", "农田耕地专项提取", "大范围耕地与农田图斑智能勾画", False, "task_farmland"),
            ("🌟", "SAM3 交互提示解译", "输入英文 Prompt 提示词智能提取", False, "task_sam3"),
            ("🐥", "深度双期影像变化检测", "AI 深度模型两期时相特征比对", False, "task_change"),
        ]

        for icon, title, desc, is_free, key in ai_cards:
            btn = TaskCardButton(icon, title, desc, is_free=is_free)
            btn.clicked.connect(lambda checked=False, k=key: self.taskSelected.emit(k))
            layout.addWidget(btn)

        layout.addStretch()


# =========================================================================
# 六、 顶层主停靠面板容器 (ImageInterpretDockWidget)
# =========================================================================
class ImageInterpretDockWidget(QDockWidget):
    def __init__(self, iface, parent=None):
        super().__init__("GeoAI 遥感智能解译", parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.plugin_dir = os.path.dirname(__file__)
        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.machine_id = get_machine_id()

        self.token = ""
        self.username = ""
        self.account_info = {}
        self.active_running_task = None

        self._build_ui()
        self._load_settings()
        self._try_restore_login()

    def _build_ui(self):
        container = QWidget()
        container.setObjectName("dockContainer")
        container.setStyleSheet("""
            QWidget#dockContainer { background-color: #f8fafc; }
            QGroupBox {
                font-weight: bold; font-size: 13px; border: 1px solid #e2e8f0;
                border-radius: 8px; margin-top: 8px; padding-top: 14px; background-color: #ffffff;
            }
            QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 6px; color: #334155; }
            QPushButton { background-color: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 12px; color: #334155; font-weight: 500; }
            QPushButton:hover { background-color: #f1f5f9; border-color: #94a3b8; }
            QPushButton#runBtn { background-color: #2563eb; color: #ffffff; border: none; font-size: 13px; font-weight: bold; }
            QPushButton#runBtn:hover { background-color: #1d4ed8; }
            QPushButton#runBtn:disabled { background-color: #cbd5e1; color: #94a3b8; }
            QPushButton#cancelBtn { background-color: #fef2f2; color: #dc2626; border: 1px solid #fca5a5; }
            QPushButton#cancelBtn:hover { background-color: #fee2e2; }
            QToolButton#iconBtn { border: none; background: transparent; font-size: 14px; padding: 4px; border-radius: 4px; }
            QToolButton#iconBtn:hover { background-color: #e2e8f0; }
            QLineEdit, QComboBox, QTextEdit, QSpinBox, QDoubleSpinBox { border: 1px solid #cbd5e1; border-radius: 6px; padding: 5px; background-color: #ffffff; color: #1e293b; }
            QTableWidget { border: 1px solid #cbd5e1; border-radius: 6px; background-color: #ffffff; gridline-color: #f1f5f9; }
            QProgressBar { border: none; background-color: #e2e8f0; border-radius: 4px; height: 8px; text-align: center; }
            QProgressBar::chunk { background-color: #2563eb; border-radius: 4px; }
            QLabel#extentLabel { background-color: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 6px; padding: 6px; font-family: monospace; font-size: 11px; color: #475569; }
            QLabel#noticeBanner { background-color: #f0f9ff; border: 1px solid #bae6fd; border-radius: 6px; padding: 6px 8px; color: #0369a1; font-size: 11px; }
        """)

        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # ---------------- 顶部全局导航栏 ----------------
        top_bar = QHBoxLayout()
        self.back_btn = QToolButton()
        self.back_btn.setObjectName("iconBtn")
        self.back_btn.setText("🐾 返回")
        self.back_btn.setToolTip("返回首页")
        self.back_btn.clicked.connect(self.show_home_page)
        self.back_btn.setVisible(False)
        top_bar.addWidget(self.back_btn)

        self.title_label = QLabel("GeoAI 遥感智能解译终端")
        self.title_label.setStyleSheet("font-size: 15px; font-weight: bold; color: #0f172a;")
        top_bar.addWidget(self.title_label)
        top_bar.addStretch()

        self.account_btn = QToolButton()
        self.account_btn.setObjectName("iconBtn")
        self.account_btn.setText("🧸 设置")
        self.account_btn.setToolTip("账号与网络设置")
        self.account_btn.clicked.connect(self.show_account_page)
        top_bar.addWidget(self.account_btn)
        main_layout.addLayout(top_bar)

        # ---------------- 多页面堆栈 ----------------
        self.stack = QStackedWidget()

        # 1. 首页
        self.home_page = HomePage()
        self.home_page.taskSelected.connect(self._navigate_to_task)
        self.stack.addWidget(self.home_page)

        # 2. 账号中心
        self.account_page = AccountSettingsPage(self)
        self.stack.addWidget(self.account_page)

        # 3. 注册所有工具页面
        self.task_pages = {}

        # 免费工具
        self.task_pages["task_spectral_index"] = ("🍀 全能光谱指数库", self.stack.addWidget(SpectralIndexTaskWidget(self)))
        self.task_pages["task_pca"] = ("🔮 PCA 主成分分析", self.stack.addWidget(PcaTransformWidget(self)))
        self.task_pages["task_dem"] = ("🗻 DEM 地形全要素分析", self.stack.addWidget(DemAnalysisWidget(self)))
        self.task_pages["task_filter"] = ("🔎 空间滤波与边缘提取", self.stack.addWidget(SpatialFilterWidget(self)))
        self.task_pages["task_area"] = ("🍰 地物分类面积统计", self.stack.addWidget(AreaStatisticsWidget(self)))
        self.task_pages["task_vector_smooth"] = ("🎀 矢量图斑化简平滑", self.stack.addWidget(VectorSmoothSimplifyWidget(self)))
        self.task_pages["task_kmeans"] = ("🍭 K-Means 智能聚类", self.stack.addWidget(KMeansClusterWidget(self)))
        self.task_pages["task_raster_diff"] = ("🐣 双期像元差分检测", self.stack.addWidget(RasterDiffChangeWidget(self)))
        self.task_pages["task_enhance"] = ("🌈 假彩色画质增强", self.stack.addWidget(ImageEnhanceWidget(self)))
        self.task_pages["task_polygonize"] = ("🧩 栅格一键矢量化", self.stack.addWidget(RasterPolygonizeWidget(self)))

        # 动态解析各类别对应的 Class ID
        building_ids = find_class_ids_by_keywords(["建筑", "住宅", "工业", "房屋", "厂房"], fallback_id="5,6,7")
        road_ids = find_class_ids_by_keywords(["路", "道", "交通"], fallback_id="4")
        water_ids = find_class_ids_by_keywords(["水", "河", "湖"], fallback_id="8")
        veg_ids = find_class_ids_by_keywords(["林", "草", "树"], fallback_id="2,3")
        farm_ids = find_class_ids_by_keywords(["耕", "农", "田"], fallback_id="1")

        # AI 大模型
        self.task_pages["task_landuse_multi"] = ("🌻 土地利用全要素解译", self.stack.addWidget(LanduseMultiTaskWidget(self)))
        self.task_pages["task_building"] = ("🏡 建筑物专项提取", self.stack.addWidget(
            SingleThemeExtractionWidget(self, building_ids, "自动识别选定区域内的城镇建筑、农村住宅与厂房轮廓。")))
        self.task_pages["task_road"] = (
        "🚗 道路交通专项提取", self.stack.addWidget(SingleThemeExtractionWidget(self, road_ids, "自动提取公路、街道、主干道及乡村道路等交通网络。")))
        self.task_pages["task_water"] = (
        "🐬 水系水体专项提取", self.stack.addWidget(SingleThemeExtractionWidget(self, water_ids, "自动识别河流、湖泊、坑塘、水库等地表水体边界。")))
        self.task_pages["task_vegetation"] = (
        "🍄 林草植被专项提取", self.stack.addWidget(SingleThemeExtractionWidget(self, veg_ids, "精准识别林地、灌木林与天然草地。")))
        self.task_pages["task_farmland"] = (
        "🥕 农田耕地专项提取", self.stack.addWidget(SingleThemeExtractionWidget(self, farm_ids, "提取农业种植用地、水浇地与旱地范围。")))
        self.task_pages["task_sam3"] = ("🌟 SAM3 交互提示解译", self.stack.addWidget(Sam3TaskWidget(self)))
        self.task_pages["task_change"] = ("🐥 深度双期变化检测", self.stack.addWidget(ChangeDetectionTaskWidget(self)))

        scroll_area = QScrollArea()
        scroll_area.setWidget(self.stack)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        main_layout.addWidget(scroll_area)

        self.setWidget(container)

    def show_home_page(self):
        self.stack.setCurrentIndex(0)
        self.title_label.setText("GeoAI 遥感智能解译终端")
        self.back_btn.setVisible(False)
        self.account_btn.setVisible(True)

    def show_account_page(self):
        self.stack.setCurrentIndex(1)
        self.title_label.setText("个人中心与设置")
        self.back_btn.setVisible(True)
        self.account_btn.setVisible(False)

    def _navigate_to_task(self, task_key: str):
        if task_key in self.task_pages:
            title, page_idx = self.task_pages[task_key]
            self.stack.setCurrentIndex(page_idx)
            self.title_label.setText(title)
            self.back_btn.setVisible(True)
            self.account_btn.setVisible(True)

    def current_server_url(self) -> str:
        if hasattr(self, 'account_page') and hasattr(self.account_page, 'server_url_edit'):
            url = self.account_page.server_url_edit.text().strip()
            if url:
                return url
        saved = self.settings.value(SETTINGS_KEY_SERVER_URL, "")
        if saved:
            return str(saved).strip()
        return DEFAULT_SERVER_URL or "http://127.0.0.1:8000"

    def _load_settings(self):
        remote_url = fetch_remote_server_url()
        is_custom = self.settings.value("is_custom_server", False, type=bool)
        saved_url = self.settings.value(SETTINGS_KEY_SERVER_URL, "")
        if is_custom and saved_url:
            self.account_page.server_url_edit.setText(str(saved_url).strip())
        else:
            self.account_page.server_url_edit.setText(
                str(remote_url or DEFAULT_SERVER_URL or "http://127.0.0.1:8000").strip())

    def _try_restore_login(self):
        token = self.settings.value(SETTINGS_KEY_TOKEN, "")
        username = self.settings.value(SETTINGS_KEY_USERNAME, "")
        if not token or not username:
            self.account_page.update_account_ui("", "", {})
            return
        self.token = str(token).strip()
        self.username = str(username).strip()
        self.refresh_account_info(silent=True)

    def open_login_dialog(self):
        dialog = LoginDialog(self.current_server_url, self)
        dialog.loggedIn.connect(self._on_logged_in)
        dialog.exec_()

    def _on_logged_in(self, username: str, token: str):
        self.username = username
        self.token = token
        self.settings.setValue(SETTINGS_KEY_TOKEN, token)
        self.settings.setValue(SETTINGS_KEY_USERNAME, username)
        self.refresh_account_info()

    def logout(self):
        self.token = "";
        self.username = "";
        self.account_info = {}
        self.settings.remove(SETTINGS_KEY_TOKEN)
        self.settings.remove(SETTINGS_KEY_USERNAME)
        self.account_page.update_account_ui("", "", {})

    def refresh_account_info(self, silent: bool = False):
        if not self.token:
            self.account_page.update_account_ui("", "", {})
            return
        client = GeoMindAuthClient(self.current_server_url(), self.token)
        try:
            self.account_info = client.get_me()
        except AuthApiError as e:
            self.logout()
            if not silent:
                QMessageBox.warning(self, "提示", f"获取账号信息失败: {e}\n\n💡 提示：可在设置页刷新网关。")
            return
        self.account_page.update_account_ui(self.token, self.username, self.account_info)

    def open_plan_dialog(self):
        if not self.token:
            QMessageBox.information(self, "提示", "请先登录后再查看套餐")
            self.open_login_dialog()
            return
        dialog = PlanDialog(self.current_server_url(), self.token, self.account_info, self.plugin_dir, self)
        dialog.exec_()
        if dialog.account_refreshed:
            self.refresh_account_info(silent=True)

    def cancel_running_task(self):
        if self.active_running_task is not None:
            try:
                self.active_running_task.cancel()
            except Exception as e:
                print(f"卸载打断任务异常: {e}")

    def closeEvent(self, event):
        self.cancel_running_task()
        super().closeEvent(event)