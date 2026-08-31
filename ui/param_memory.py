# -*- coding: utf-8 -*-
"""
Generic parameter snapshot helpers for the memory / history feature.

``collect_param_snapshot`` walks a page widget tree and records the value of
every parameter control that has a non-empty objectName (combo boxes, spins,
checkboxes, text inputs, layer pickers).  ``apply_param_snapshot`` restores
them by objectName — layer pickers are resolved from the current QGIS project
by layer id.  This lets every local tool page get parameter prefill for free
after its controls are given objectNames.
"""
from typing import Any, Dict, Optional

from qgis.PyQt.QtWidgets import (
    QComboBox, QSpinBox, QDoubleSpinBox, QSlider, QCheckBox,
    QLineEdit, QTextEdit, QPlainTextEdit,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.core import QgsProject

#: Widget types whose values are captured (value controls only, not labels/buttons).
_CAPTURE_CLASSES = (
    QComboBox, QSpinBox, QDoubleSpinBox, QSlider,
    QCheckBox, QLineEdit, QTextEdit, QPlainTextEdit,
)

_LAYER_COMBO_CLASS = QgsMapLayerComboBox


def _collect(widget, snapshot: dict) -> None:
    name = widget.objectName()
    if not name:
        return
    if isinstance(widget, _LAYER_COMBO_CLASS):
        layer = widget.currentLayer()
        snapshot[name] = {
            "type": "layer",
            "layer_id": layer.id() if layer else "",
        }
    elif isinstance(widget, QComboBox):
        snapshot[name] = {
            "type": "combo",
            "data": widget.currentData(),
            "index": widget.currentIndex(),
        }
    elif isinstance(widget, (QSpinBox, QDoubleSpinBox, QSlider)):
        snapshot[name] = {"type": "spin", "value": widget.value()}
    elif isinstance(widget, QCheckBox):
        snapshot[name] = {"type": "check", "checked": widget.isChecked()}
    elif isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
        snapshot[name] = {"type": "text", "text": widget.toPlainText() if isinstance(widget, (QTextEdit, QPlainTextEdit)) else widget.text()}


def collect_param_snapshot(page_widget) -> Dict[str, Any]:
    """Capture the current values of all named parameter controls."""
    snapshot: Dict[str, Any] = {}
    for child in page_widget.findChildren(_CAPTURE_CLASSES):
        _collect(child, snapshot)
    return snapshot


def flatten_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce typed snapshot entries to plain scalars for memory stats.

    ``{"index_combo": {"type": "combo", "data": "ndvi"}}`` becomes
    ``{"index_combo": "ndvi"}`` so the memory store can count frequency.
    """
    flat: Dict[str, Any] = {}
    for name, entry in snapshot.items():
        if not isinstance(entry, dict) or entry.get("type") is None:
            flat[name] = entry
            continue
        kind = entry.get("type")
        if kind == "combo":
            value = entry.get("data")
        elif kind == "layer":
            value = entry.get("layer_id") or ""
        elif kind == "spin":
            value = entry.get("value")
        elif kind == "check":
            value = entry.get("checked")
        elif kind == "text":
            value = entry.get("text")
        else:
            value = None
        if value is not None and value != "":
            flat[name] = value
    return flat


def _normalize(name: str, widget, value: Any) -> Optional[dict]:
    """Coerce a raw scalar (from memory suggestions) into a typed entry."""
    if isinstance(value, dict) and "type" in value:
        return dict(value)
    if isinstance(widget, _LAYER_COMBO_CLASS):
        return {"type": "layer", "layer_id": value or ""}
    if isinstance(widget, QComboBox):
        return {"type": "combo", "data": value, "index": -1}
    if isinstance(widget, (QSpinBox, QDoubleSpinBox, QSlider)):
        try:
            return {"type": "spin", "value": float(value)}
        except (TypeError, ValueError):
            return None
    if isinstance(widget, QCheckBox):
        if isinstance(value, str):
            checked = value.strip().lower() in ("1", "true", "yes", "on")
        else:
            checked = bool(value)
        return {"type": "check", "checked": checked}
    if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
        return {"type": "text", "text": str(value or "")}
    return None


def _apply_one(widget, entry: Optional[dict]) -> bool:
    if not entry:
        return False
    kind = entry.get("type")
    if isinstance(widget, _LAYER_COMBO_CLASS) and kind == "layer":
        layer_id = entry.get("layer_id", "")
        if layer_id:
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None:
                widget.setLayer(layer)
                return True
    elif isinstance(widget, QComboBox) and kind == "combo":
        data = entry.get("data")
        idx = widget.findData(data)
        if idx >= 0:
            widget.setCurrentIndex(idx)
            return True
        index = entry.get("index")
        if isinstance(index, int) and 0 <= index < widget.count():
            widget.setCurrentIndex(index)
            return True
    elif isinstance(widget, (QSpinBox, QDoubleSpinBox, QSlider)) and kind == "spin":
        try:
            widget.setValue(float(entry.get("value", 0)))
            return True
        except (TypeError, ValueError):
            return False
    elif isinstance(widget, QCheckBox) and kind == "check":
        widget.setChecked(bool(entry.get("checked", False)))
        return True
    elif isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)) and kind == "text":
        text = str(entry.get("text", ""))
        if isinstance(widget, (QTextEdit, QPlainTextEdit)):
            widget.setPlainText(text)
        else:
            widget.setText(text)
        return True
    return False


def apply_param_snapshot(page_widget, snapshot: Dict[str, Any]) -> int:
    """Restore saved values by objectName. Returns the number restored.

    Ordering matters: dependent controls (e.g. spin boxes reset by a layer or
    combo change) are applied after the controls that trigger them, with
    signals blocked so the reset handlers don't clobber the restored values.
    """
    if not snapshot:
        return 0
    by_name: Dict[str, QComboBox] = {
        child.objectName(): child
        for child in page_widget.findChildren(_CAPTURE_CLASSES)
        if child.objectName()
    }

    def apply_group(names, block_signals: bool) -> int:
        restored = 0
        for name in names:
            widget = by_name[name]
            entry = _normalize(name, widget, snapshot.get(name))
            widget.blockSignals(block_signals)
            try:
                if _apply_one(widget, entry):
                    restored += 1
            finally:
                widget.blockSignals(False)
        return restored

    check_text = [n for n in by_name if isinstance(by_name[n], (QCheckBox, QLineEdit, QTextEdit, QPlainTextEdit))]
    combos = [n for n in by_name if isinstance(by_name[n], QComboBox) and not isinstance(by_name[n], _LAYER_COMBO_CLASS)]
    layers = sorted(n for n in by_name if isinstance(by_name[n], _LAYER_COMBO_CLASS))
    spins = [n for n in by_name if isinstance(by_name[n], (QSpinBox, QDoubleSpinBox, QSlider))]

    restored = 0
    restored += apply_group(check_text, False)
    restored += apply_group(combos + layers, False)
    restored += apply_group(spins, True)
    return restored
