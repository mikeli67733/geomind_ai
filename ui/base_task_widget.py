# -*- coding: utf-8 -*-
"""
Base task widget and AI deep-learning interpretation task widgets.

BaseTaskWidget provides the common UI scaffold (layer selection, extent
selection, progress bar, cancel button) that all AI task widgets inherit.
"""
import os
import tempfile
from datetime import datetime

from osgeo import gdal

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QComboBox, QPushButton, QTextEdit, QProgressBar, QMessageBox,
    QGroupBox, QCheckBox, QApplication,
)
from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer,
    QgsRectangle, QgsCoordinateTransform, QgsApplication,
)
from qgis.gui import QgsMapLayerComboBox

from ..core.compat import RASTER_LAYER_FILTER
from ..core.constants import (
    LANDUSE_CLASSES, DEFAULT_CHECKED_CLASS_IDS, get_model_key_by_mode,
)
from ..utils.extent_tool import ExtentSelectTool
from ..tasks.interpret_task import InterpretTask


class BaseTaskWidget(QWidget):
    """Common scaffold for all cloud-based AI interpretation tasks."""

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

        # 1. Input image
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

        # 2. Parameters
        self.param_group = QGroupBox("2. 任务参数配置")
        self.param_layout = QVBoxLayout(self.param_group)
        self.build_parameters_ui(self.param_layout)
        layout.addWidget(self.param_group)

        # 3. Extent selection
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

        # 4. Execute
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

    # -- Overrides for subclasses -------------------------------------------

    def build_parameters_ui(self, layout: QVBoxLayout):
        pass

    def get_task_parameters(self) -> dict:
        return {}

    # -- Extent selection ---------------------------------------------------

    def _activate_extent_tool(self):
        self.extent_tool = ExtentSelectTool(self.canvas)
        self.extent_tool.extentSelected.connect(self._on_extent_selected)
        self.canvas.setMapTool(self.extent_tool)
        self.status_label.setText("请在地图上按住左键拖拽框选范围（右键/Esc取消）")

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
        self.status_label.setText("已选定解译范围")

    # -- Task execution -----------------------------------------------------

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
        if params is None:
            return

        canvas_crs = self.canvas.mapSettings().destinationCrs()

        # Extent size guard: stop clipping/interpretation when too large.
        from ..utils.extent_guard import check_extent_too_large

        too_large, guard_msg = check_extent_too_large(
            layer_t1, self.selected_extent, canvas_crs
        )
        if too_large:
            reply = QMessageBox.warning(
                self, "解译范围过大",
                f"{guard_msg}\n\n为避免浪费解译时间与额度，已停止推送裁图与解译。\n\n是否仍然继续执行？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self.status_label.setText("已停止：解译范围过大，请缩小范围后重试")
                return

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

    # -- Task callbacks -----------------------------------------------------

    def _on_finished_ok(self, result_path, content_type):
        self._set_running_state(False)
        self.task = None
        self.dock.active_running_task = None
        time_display = datetime.now().strftime("%H:%M:%S")
        layer_name = f"解译结果_{self.model_key}({time_display})"

        if result_path.endswith(".tif"):
            new_layer = QgsRasterLayer(result_path, layer_name)
        else:
            new_layer = QgsVectorLayer(result_path, layer_name, "ogr")

        if not new_layer.isValid():
            QMessageBox.critical(self, "错误", "解译结果加载失败")
            self.status_label.setText("加载结果失败")
            return

        if hasattr(self, "_task_canvas_extent") and hasattr(self, "_task_canvas_crs"):
            clipped = self._clip_layer_to_extent(
                new_layer, result_path,
                QgsRectangle(self._task_canvas_extent),
                self._task_canvas_crs, layer_name,
            )
            if clipped and clipped.isValid():
                new_layer = clipped

        QgsProject.instance().addMapLayer(new_layer)
        self.status_label.setText(f"解译完成！已加载图层: {layer_name}")
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
        """Clip the result layer to the original selection extent."""
        try:
            layer_crs = layer.crs()
            if extent_crs != layer_crs:
                transform = QgsCoordinateTransform(extent_crs, layer_crs, QgsProject.instance())
                extent = transform.transformBoundingBox(extent)
        except Exception:
            return None

        if isinstance(layer, QgsRasterLayer):
            try:
                out_path = os.path.join(tempfile.gettempdir(), f"clipped_{id(self)}.tif")
                proj_win = [extent.xMinimum(), extent.yMaximum(), extent.xMaximum(), extent.yMinimum()]
                ds = gdal.Translate(out_path, result_path, options=gdal.TranslateOptions(projWin=proj_win))
                ds = None
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return QgsRasterLayer(out_path, layer_name)
            except Exception:
                pass
        else:
            try:
                from qgis import processing
                result = processing.run("native:extractbyextent", {
                    "INPUT": result_path, "EXTENT": extent, "OUTPUT": "memory:",
                })
                clipped = result["OUTPUT"]
                if clipped and clipped.isValid():
                    clipped.setName(layer_name)
                    return clipped
            except Exception:
                pass
        return None


# ===========================================================================
# AI deep-learning task widgets
# ===========================================================================

class LanduseMultiTaskWidget(BaseTaskWidget):
    """Land use multi-class interpretation widget."""

    def __init__(self, main_dock, parent=None):
        real_key = get_model_key_by_mode("landuse")
        super().__init__(main_dock, model_key=real_key, mode="landuse", parent=parent)

    def build_parameters_ui(self, layout: QVBoxLayout):
        self.param_group.setTitle("2. 选择解译要素 (多选)")
        class_grid = QGridLayout()
        self.class_checkboxes = {}
        for idx, (label, cls_id) in enumerate(LANDUSE_CLASSES):
            cb = QCheckBox(label)
            if cls_id in DEFAULT_CHECKED_CLASS_IDS:
                cb.setChecked(True)
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
    """Single-theme extraction widget (building, water, road, etc.)."""

    def __init__(self, main_dock, target_class_id: str, desc: str, parent=None):
        self.fixed_target_class = target_class_id
        self.desc_text = desc
        real_key = get_model_key_by_mode("landuse")
        super().__init__(main_dock, model_key=real_key, mode="landuse", parent=parent)

    def build_parameters_ui(self, layout: QVBoxLayout):
        self.param_group.setTitle("2. 专项提取说明")
        lbl = QLabel(self.desc_text)
        lbl.setStyleSheet("color: #475569; font-size: 12px;")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

    def get_task_parameters(self) -> dict:
        return {"target_class": self.fixed_target_class, "output_format": "mask"}


class Sam3TaskWidget(BaseTaskWidget):
    """SAM3 prompt-driven interpretation widget."""

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
    """Dual-period change detection widget."""

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
