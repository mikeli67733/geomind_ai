# -*- coding: utf-8 -*-
"""
Base widget for local (offline) GIS/RS processing tools.

Every tool page shares the same scaffold: a reference to the main dock
and canvas, an optional extent-selection tool, and a ``_build_ui`` hook.
On top of that, the base class wires every page into the history / memory
system:

- a per-page 「🕘 历史」 button that jumps to the history page filtered to
  this tool;
- ``record_local_run`` — subclasses call it at the end of their run
  methods to archive parameters / outputs and feed the memory store;
- ``apply_saved_params`` — restores remembered parameters (most-used or
  last-used values) when the page is first shown.
"""
import time

from qgis.PyQt.QtWidgets import QWidget, QPushButton, QHBoxLayout
from qgis.gui import QgsMapLayerComboBox

from ...core.history import history_store
from ...core.memory import memory_store
from ..param_memory import (
    collect_param_snapshot,
    apply_param_snapshot,
    flatten_snapshot,
)

_STATUS_TEXT = {"ok": "成功", "failed": "失败", "cancelled": "已取消"}


class BaseLocalToolWidget(QWidget):
    """Common scaffold for local (offline) processing tools."""

    def __init__(self, main_dock, parent=None):
        super().__init__(parent)
        self.dock = main_dock
        self.canvas = main_dock.canvas
        self.extent_tool = None
        self.selected_extent = None
        self.page_key = None      # assigned by the dock registry
        self.page_title = None
        self._memory_applied = False
        self._run_started_at = None
        self._layer_user_picked = False
        self._build_ui()
        self._add_history_button()
        # ``activated`` only fires on real user interaction, never on
        # programmatic setLayer — used to honour the user's own choice.
        for combo in self.findChildren(QgsMapLayerComboBox):
            combo.activated.connect(self._on_layer_user_picked)

    def _on_layer_user_picked(self, _index):
        self._layer_user_picked = True

    def _build_ui(self):
        """Override in subclasses to construct the UI."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # History / memory integration
    # ------------------------------------------------------------------
    def _add_history_button(self):
        """Insert a small 「历史」 button at the top of the page layout."""
        layout = self.layout()
        if layout is None:
            return
        row = QHBoxLayout()
        row.setSpacing(6)
        self.history_btn = QPushButton("🕘 历史")
        self.history_btn.setFixedWidth(72)
        self.history_btn.setToolTip("查看该工具页的历史运行记录")
        self.history_btn.clicked.connect(self._show_page_history)
        row.addWidget(self.history_btn)
        row.addStretch()
        layout.insertLayout(0, row)

    def _show_page_history(self):
        self.dock.show_history_page(filter_key=self.page_key or "")

    def showEvent(self, event):
        """Prefill remembered parameters and adopt the home-page selection."""
        super().showEvent(event)
        if self.page_key and not self._memory_applied:
            self._memory_applied = True
            suggested = memory_store.suggested_params(self.page_key)
            if suggested:
                self.apply_saved_params(suggested)
                status = getattr(self, "status_label", None)
                if status is not None:
                    status.setText(
                        "🧠 已按您的使用习惯预填参数（可在历史页查看记忆详情）")
        # A fresh view of this page: honour the home-page global selection
        # again unless the user already picked a layer by hand.
        self._layer_user_picked = False
        self.apply_global_selection()

    def apply_global_selection(self):
        """Adopt the home-page global layer/extent unless the user chose one.

        Only the first layer picker of the page is touched, and only when it
        is not the home-page layer; QgsMapLayerComboBox already filters by
        type, so a mismatched global layer is simply ignored. The extent is
        applied only on pages that support extent selection.
        """
        try:
            layer = getattr(self.dock, "global_layer", None)
            combos = self.findChildren(QgsMapLayerComboBox)
            if combos and layer is not None and not self._layer_user_picked:
                current = combos[0].currentLayer()
                if current is None or current != layer:
                    combos[0].setLayer(layer)
            extent = getattr(self.dock, "global_extent", None)
            if (extent is not None and self.selected_extent is None
                    and hasattr(self, "_on_extent_selected")):
                self._on_extent_selected(extent)
        except Exception as exc:
            from ...core.logger import get_logger
            get_logger(__name__).warning(
                "Failed to apply home-page selection: %s", exc)

    def apply_saved_params(self, params: dict):
        """Restore saved parameter values by objectName."""
        if params:
            apply_param_snapshot(self, params)

    def record_local_run(
        self,
        status: str,
        summary: str = "",
        params: dict = None,
        output_paths: list = None,
        error: str = "",
    ):
        """Archive one local run to history and feed the memory store."""
        if not self.page_key:
            return
        snapshot = params if params is not None else collect_param_snapshot(self)
        layers = {}
        for name, entry in snapshot.items():
            if isinstance(entry, dict) and entry.get("type") == "layer":
                layers[name] = entry.get("layer_id", "")

        duration_ms = 0
        if self._run_started_at is not None:
            duration_ms = int((time.time() - self._run_started_at) * 1000)

        history_store.record_run(
            page_key=self.page_key,
            page_title=self.page_title or self.page_key,
            status=status,
            params=snapshot,
            summary=summary or f"{self.page_title or self.page_key} {_STATUS_TEXT.get(status, status)}",
            input_layers=self._current_input_layers(),
            output_files=output_paths or [],
            duration_ms=duration_ms,
            error=error,
        )
        memory_store.record_usage(
            page_key=self.page_key,
            params=flatten_snapshot(snapshot),
            layers=layers,
        )

    def _current_input_layers(self) -> list:
        """Snapshot the layers currently picked in the page's layer pickers."""
        layers = []
        for combo in self.findChildren(QgsMapLayerComboBox):
            layer = combo.currentLayer()
            if layer is not None:
                layers.append({
                    "name": layer.name(),
                    "id": layer.id(),
                    "source": layer.source(),
                })
        return layers

    def mark_run_started(self):
        """Call at the start of a run method so duration can be measured."""
        self._run_started_at = time.time()
