# -*- coding: utf-8 -*-
"""
Central configuration and constants for GeoMind AI.

All tunable values — API endpoints, model definitions, plan settings,
UI strings — are declared here so they can be changed in one place.
"""
import os
from typing import List, Tuple

from .logger import get_logger

logger = get_logger("constants")

# ===========================================================================
# Server configuration
#
# NOTE: resolution of the effective backend URL is lazy and lives in
# ``core.config.Settings`` — importing this module must never hit the
# network (see resolution chain: QSettings > server_config.json >
# remote config > FALLBACK_SERVER_URL).
# ===========================================================================

REMOTE_CONFIG_URLS: List[str] = [
    "https://m1.apifoxmock.com/m1/8731923-8518910-default/config",
]

FALLBACK_SERVER_URL = "http://127.0.0.1:8000"

# ===========================================================================
# AI model definitions
# ===========================================================================

MODELS: List[Tuple[str, str, str]] = [
    ("双期影像变化检测 (CHANGE_DETECTION)", "CHANGE_DETECTION", "change_detection"),
    ("土地利用/多要素识别 (LANDUSE)", "LANDUSE", "landuse"),
    ("SAM3 提示词通用大模型 (SAM3)", "SAM3_MODEL", "sam3"),
]

LANDUSE_CLASSES: List[Tuple[str, int]] = [
    ("耕地", 1),
    ("林地", 3),
    ("草地", 4),
    ("建筑", 5),
    ("道路", 6),
    ("施工", 8),
    ("水体", 10),
]

DEFAULT_CHECKED_CLASS_IDS = {10}

# ===========================================================================
# API endpoint paths
# ===========================================================================

API_SUBMIT = "/api/v1/task/submit"
API_STATUS = "/api/v1/task/status/{task_id}"
API_RESULT = "/api/v1/task/result/{task_id}"
API_CANCEL = "/api/v1/task/cancel/{task_id}"

API_REGISTER = "/api/v1/auth/register"
API_LOGIN = "/api/v1/auth/login"
API_USER_ME = "/api/v1/user/me"
API_PAYMENT_REDEEM = "/api/v1/payment/redeem"
API_CHANGE_PASSWORD = "/api/v1/auth/change-password"
API_COPILOT_CHAT = "/api/v1/copilot/chat"

# ===========================================================================
# External service configuration
# ===========================================================================

# Tianditu geocoding API — key should be set via environment variable
TIANDITU_API_KEY = os.environ.get("GEOMIND_TIANDITU_TK", "7ba1ada42adefb5df42e4a1364b321c4")
TIANDITU_GEOCODER_URL = "https://api.tianditu.gov.cn/geocoder"

XIANYU_PRODUCT_URL = "https://m.tb.cn/h.8SfKfsd?tk=u3XFgAMYbS2"

# ===========================================================================
# Default operational parameters
# ===========================================================================

DEFAULT_POLL_INTERVAL = 2.0
CANCEL_CHECK_INTERVAL = 0.3
DEFAULT_TIMEOUT = 600
JPEG_QUALITY = 75

PLUGIN_TASK_DESCRIPTION = "GeoMind AI 遥感影像智能解译"

# ===========================================================================
# Extent size guard (解译范围保护)
#
# 当画布/框选范围相对栅格分辨率折算出的像素规模超过上限时，停止推送
# 裁图与解译，避免裁剪超时、上传失败与解译质量下降。
#
# 上限按"服务器带宽"收紧：500 万像素 + JPEG q75 约 3~5 MB，普通带宽可承受；
# 若仍嫌大，可继续调小（如 300 万 / 单边 3,000）。
# ===========================================================================

MAX_EXTENT_PIXELS = 5_000_000       # 总像素上限（约 500 万像素）
MAX_EXTENT_SIDE_PIXELS = 4_000      # 单边像素上限（避免超长条带范围）

# ===========================================================================
# Plan and quota settings
# ===========================================================================

FREE_PLAN_DAILY_QUOTA = 20
PRO_PLAN_PRICE_YUAN = 99
PRO_PLAN_DAYS = 30
CUSTOM_PLAN_CONTACT_TEXT = "定制服务 / 私有化部署按解译面积报价，请扫码联系作者"

PLAN_LABELS = {
    "free": "免费版",
    "pro": "包月会员 (PRO)",
    "custom": "定制版/私有化部署",
}

# ===========================================================================
# QSettings keys
# ===========================================================================

SETTINGS_ORG = "GeoMindAI"
SETTINGS_APP = "Plugin"
SETTINGS_KEY_SERVER_URL = "server_url"
SETTINGS_KEY_LICENSE_KEY = "license_key"
SETTINGS_KEY_TOKEN = "auth_token"
SETTINGS_KEY_USERNAME = "auth_username"
SETTINGS_KEY_LLM_API_KEY = "llm_api_key"
SETTINGS_KEY_LLM_BASE_URL = "llm_base_url"
SETTINGS_KEY_LLM_MODEL = "llm_model"

# Dock widget state persistence (restored after QGIS restart)
SETTINGS_KEY_DOCK_VISIBLE = "ui/dock_visible"
SETTINGS_KEY_DOCK_PAGE = "ui/dock_page"
SETTINGS_KEY_DOCK_AREA = "ui/dock_area"

DEFAULT_LLM_BASE_URL = "https://api.deepseek.com"
DEFAULT_LLM_MODEL = "deepseek-v4-flash"

# ===========================================================================
# Helper functions
# ===========================================================================


def find_class_ids_by_keywords(keywords: List[str], fallback_id: str = "") -> str:
    """Look up class IDs from LANDUSE_CLASSES by matching keywords."""
    matched = []
    for label, cls_id in LANDUSE_CLASSES:
        for kw in keywords:
            if kw in label:
                matched.append(str(cls_id))
                break
    return ",".join(matched) if matched else fallback_id


def get_model_key_by_mode(target_mode: str, fallback_key: str = "") -> str:
    """Resolve the backend model_key from MODELS by mode identifier."""
    for item in MODELS:
        if len(item) >= 3 and item[2] == target_mode:
            return item[1]
    if MODELS and len(MODELS[0]) >= 2:
        return MODELS[0][1]
    return fallback_key
