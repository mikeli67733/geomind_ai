# -*- coding: utf-8 -*-
"""
Central settings facade for GeoMind AI.

Replaces the import-time side effects that previously lived in
``core.constants`` (a blocking HTTP fetch that ran the moment any module
was imported). All tunable runtime values are now resolved lazily here,
so loading the plugin never blocks on the network.

Backend URL & Xianyu URL resolution chain (first hit wins):
    1. user override in QSettings            (SETTINGS_KEY_SERVER_URL) [仅 server_url]
    2. local ``server_config.json``          (shipped with the plugin)
    3. remote config endpoint                (lazy fetch, cached in-process)
    4. built-in fallback URL                 (FALLBACK_SERVER_URL / FALLBACK_XIANYU_URL)
"""
import json
import os
import threading
import time
from typing import Optional, Dict, Any

from .constants import (
    FALLBACK_SERVER_URL,
    FALLBACK_XIANYU_URL,
    REMOTE_CONFIG_URLS,
    SETTINGS_APP,
    SETTINGS_KEY_SERVER_URL,
    SETTINGS_ORG,
)
from .logger import get_logger

logger = get_logger(__name__)

_PLUGIN_DIR_CACHE: Optional[str] = None


def _plugin_dir() -> str:
    """Resolve the plugin root directory (``geomind_ai/``) exactly once."""
    global _PLUGIN_DIR_CACHE
    if _PLUGIN_DIR_CACHE is None:
        # core/config.py -> geomind_ai/core/ -> geomind_ai/
        _PLUGIN_DIR_CACHE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _PLUGIN_DIR_CACHE


def _fetch_remote_config() -> Optional[Dict[str, Any]]:
    """Fetch the remote config dict with multi-source fallback.

    Called lazily (never at import time) and only when no local source
    resolved the backend URL first.
    """
    timestamp = int(time.time())
    for url in REMOTE_CONFIG_URLS:
        try:
            import requests
            cache_buster_url = f"{url}?_t={timestamp}"
            resp = requests.get(cache_buster_url, timeout=4)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("Failed to fetch config from %s: %s", url, exc)
            continue
    logger.warning("All remote config sources failed, using fallback config")
    return None


class Settings:
    """Lazy, layered configuration accessor (module-level singleton below)."""

    def __init__(self, plugin_dir: Optional[str] = None):
        self._plugin_dir = plugin_dir or _plugin_dir()
        self._cached_remote_config: Optional[Dict[str, Any]] = None
        self._remote_fetched = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Backend URL resolution
    # ------------------------------------------------------------------
    def server_url(self, force_refresh: bool = False) -> str:
        """Return the effective backend URL without ever blocking import."""
        override = self._user_override()
        if override:
            return override
        local = self._local_file_value("server_url")
        if local:
            return local
        remote = self._remote_config(force_refresh=force_refresh).get("server_url")
        if remote:
            return str(remote).strip().rstrip("/")
        return FALLBACK_SERVER_URL

    # ------------------------------------------------------------------
    # Xianyu URL resolution
    # ------------------------------------------------------------------
    def xianyu_url(self, force_refresh: bool = False) -> str:
        """Return the effective Xianyu product purchase URL."""
        local = self._local_file_value("xianyu_url")
        if local:
            return local
        remote = self._remote_config(force_refresh=force_refresh).get("xianyu_url")
        if remote:
            return str(remote).strip()
        return FALLBACK_XIANYU_URL

    def _user_override(self) -> Optional[str]:
        """QSettings override configured by the user in the settings page."""
        try:
            from qgis.PyQt.QtCore import QSettings
            raw = QSettings(SETTINGS_ORG, SETTINGS_APP).value(SETTINGS_KEY_SERVER_URL, "")
            val = str(raw or "").strip().rstrip("/")
            return val or None
        except Exception:
            return None

    def _local_file_value(self, key: str) -> Optional[str]:
        """Read a specific key from ``server_config.json`` shipped in the plugin directory."""
        try:
            path = os.path.join(self._plugin_dir, "server_config.json")
            if not os.path.isfile(path):
                return None
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            val = data.get(key)
            return str(val).strip() if val else None
        except Exception as exc:
            logger.debug("Failed to read key %s from server_config.json: %s", key, exc)
            return None

    def _remote_config(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Lazy remote fetch with thread-safe in-process caching."""
        if not force_refresh and self._remote_fetched:
            return self._cached_remote_config or {}
        with self._lock:
            if not force_refresh and self._remote_fetched:
                return self._cached_remote_config or {}
            self._cached_remote_config = _fetch_remote_config()
            self._remote_fetched = True
            return self._cached_remote_config or {}


# Module-level singleton consumed across the code base.
settings = Settings()