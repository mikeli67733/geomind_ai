# -*- coding: utf-8 -*-
"""
Central logging configuration for GeoMind AI.

Provides a singleton logger that routes messages to the QGIS message bar
(when running inside QGIS) and to stderr (for standalone testing).
"""
import logging
import sys
from typing import Optional

_LOGGER_NAME = "GeoMindAI"
_initialized = False


def _init_logger() -> logging.Logger:
    global _initialized
    logger = logging.getLogger(_LOGGER_NAME)
    if _initialized:
        return logger

    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — always available
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    _initialized = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a module-scoped child logger under the GeoMind AI root."""
    root = _init_logger()
    if name:
        return root.getChild(name)
    return root


def log_to_qgis(message: str, level: int = logging.INFO) -> None:
    """Push a message to the QGIS message bar if available, otherwise log normally."""
    try:
        from qgis.utils import iface
        if iface is not None:
            from qgis.core import Qgis
            qgis_level = {
                logging.DEBUG: Qgis.Info,
                logging.INFO: Qgis.Info,
                logging.WARNING: Qgis.Warning,
                logging.ERROR: Qgis.Critical,
                logging.CRITICAL: Qgis.Critical,
            }.get(level, Qgis.Info)
            iface.messageBar().pushMessage("GeoMind AI", message, level=qgis_level, duration=5)
            return
    except Exception:
        pass
    get_logger().log(level, message)
