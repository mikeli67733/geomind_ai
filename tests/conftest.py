# -*- coding: utf-8 -*-
"""Shared pytest fixtures: make the ``geomind_ai`` package importable."""
import os
import sys

# tests/conftest.py -> tests/ -> geomind_ai/ (package root).
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# QGIS loads the plugin as ``geomind_ai`` from its parent directory; mirror
# that layout so tests import the exact same package.
_PKG_PARENT = os.path.dirname(_PKG_ROOT)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)
