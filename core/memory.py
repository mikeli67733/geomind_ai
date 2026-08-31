# -*- coding: utf-8 -*-
"""
Memory store — the "gets smarter the more you use it" layer.

Every successful/failed run feeds ``record_usage``, which keeps three kinds
of signals per page key:

    last_params    most recent parameter snapshot (quick prefill)
    freq           per-field value frequency (most-used-value prefill)
    last_layers    most recent input layer ids (auto re-select)

``suggested_params`` merges them into a best-effort prefill: the most
frequent value wins, otherwise the last used value.  The history page uses
``most_used_pages`` / ``recently_used`` to surface what the user does most.
"""
import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .logger import get_logger

logger = get_logger("core.memory")

_MEMORY_FILE = "memory.json"
_MAX_RECENT = 20
_RECENT_PARAMS_WINDOW = 10
#: Recent runs vote with weight DECAY ** age (0 = latest); combined with the
#: long-term frequency count so established habits are not drowned by one
#: recent experiment, but a new habit overtakes an old one within a few runs.
_DECAY = 0.7
_FREQ_WEIGHT = 0.5


def _data_root() -> str:
    """Same per-user GeoMind data dir as core.history."""
    from qgis.core import QgsApplication
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "geomind_ai")


class MemoryStore:
    """JSON-backed per-user memory of parameters, layers and usage stats."""

    def __init__(self, base_dir: Optional[str] = None):
        # The QGIS profile dir is resolved lazily so importing this module
        # never requires a running QGIS (same pattern as core.config).
        self._base_dir = base_dir
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "version": 1,
            "titles": {},
            "usage": {},
        }
        self._loaded = False
        if base_dir is not None:
            self._load()

    @property
    def base_dir(self) -> str:
        if self._base_dir is None:
            self._base_dir = _data_root()
        return self._base_dir

    @property
    def path(self) -> str:
        return os.path.join(self.base_dir, _MEMORY_FILE)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()
            self._loaded = True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._data = data
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.debug("Failed to save memory: %s", exc)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def remember_page_title(self, page_key: str, title: str) -> None:
        self._ensure_loaded()
        if title and self._data["titles"].get(page_key) != title:
            self._data["titles"][page_key] = title
            self._save()

    def record_usage(
        self,
        page_key: str,
        params: Optional[dict] = None,
        layers: Optional[dict] = None,
    ) -> None:
        """Feed one run's parameters/layers into the memory."""
        self._ensure_loaded()
        with self._lock:
            usage = self._data["usage"].setdefault(page_key, {})
            usage["count"] = int(usage.get("count", 0)) + 1
            usage["last_used_at"] = datetime.now().isoformat(timespec="seconds")

            recent = list(usage.get("recent_at", []))
            recent.append(usage["last_used_at"])
            usage["recent_at"] = recent[-_MAX_RECENT:]

            if params:
                scalar_params = {
                    str(k): s for k, v in params.items()
                    if (s := self._scalar(v)) is not None
                }
                usage["last_params"] = scalar_params
                # Rolling window of recent runs, newest last — drives the
                # recency-weighted suggestion below.
                recent_params = list(usage.get("recent_params", []))
                recent_params.append(scalar_params)
                usage["recent_params"] = recent_params[-_RECENT_PARAMS_WINDOW:]

                freq = usage.setdefault("freq", {})
                for key, value in params.items():
                    field = freq.setdefault(str(key), {})
                    token = self._freq_token(value)
                    if token is not None:
                        field[token] = int(field.get(token, 0)) + 1

            if layers:
                usage["last_layers"] = {str(k): str(v) for k, v in layers.items()}
            self._save()

    @staticmethod
    def _scalar(value: Any) -> Any:
        """Keep small JSON-safe scalars; drop complex objects/dicts."""
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return None

    @staticmethod
    def _freq_token(value: Any) -> Optional[str]:
        if value is None or value == "" or isinstance(value, (dict, list, tuple)):
            return None
        return str(value)

    # ------------------------------------------------------------------
    # Reading / suggestion
    # ------------------------------------------------------------------
    def last_params(self, page_key: str) -> dict:
        self._ensure_loaded()
        usage = self._data["usage"].get(page_key) or {}
        return dict(usage.get("last_params") or {})

    def last_layers(self, page_key: str) -> dict:
        self._ensure_loaded()
        usage = self._data["usage"].get(page_key) or {}
        return dict(usage.get("last_layers") or {})

    def suggested_params(self, page_key: str) -> dict:
        """Best-effort parameter prefill, "gets smarter the more you use it".

        Score per candidate value = recency-weighted votes from the rolling
        window of recent runs (weight ``_DECAY ** age``) plus a discounted
        long-term frequency count.  A value used heavily long ago still has a
        base score, but a new habit overtakes it within a few recent runs.
        """
        self._ensure_loaded()
        usage = self._data["usage"].get(page_key) or {}
        recent_params = list(usage.get("recent_params") or [])
        freq = usage.get("freq") or {}

        scores: Dict[str, Dict[str, float]] = {}
        for age, snap in enumerate(reversed(recent_params)):
            weight = _DECAY ** age
            if not isinstance(snap, dict):
                continue
            for field, value in snap.items():
                if value is None or value == "":
                    continue
                field_scores = scores.setdefault(field, {})
                token = str(value)
                field_scores[token] = field_scores.get(token, 0.0) + weight

        for field, counts in freq.items():
            if not isinstance(counts, dict):
                continue
            field_scores = scores.setdefault(field, {})
            for token, count in counts.items():
                field_scores[token] = field_scores.get(token, 0.0) + _FREQ_WEIGHT * count

        return {
            field: max(field_scores, key=field_scores.get)
            for field, field_scores in scores.items()
            if field_scores
        }

    def page_title(self, page_key: str) -> str:
        self._ensure_loaded()
        return self._data["titles"].get(page_key) or page_key

    def usage_count(self, page_key: str) -> int:
        self._ensure_loaded()
        usage = self._data["usage"].get(page_key) or {}
        return int(usage.get("count", 0))

    def most_used_pages(self, top_n: int = 3) -> List[Tuple[str, int]]:
        self._ensure_loaded()
        items = [
            (key, int(usage.get("count", 0)))
            for key, usage in self._data["usage"].items()
        ]
        items.sort(key=lambda kv: kv[1], reverse=True)
        return items[:top_n]

    def recently_used(self, top_n: int = 3) -> List[Tuple[str, str]]:
        self._ensure_loaded()
        items = [
            (key, usage.get("last_used_at", ""))
            for key, usage in self._data["usage"].items()
            if usage.get("last_used_at")
        ]
        items.sort(key=lambda kv: kv[1], reverse=True)
        return items[:top_n]

    def stats(self) -> dict:
        self._ensure_loaded()
        total = sum(
            int(usage.get("count", 0)) for usage in self._data["usage"].values()
        )
        return {
            "total_runs": total,
            "pages_touched": len(self._data["usage"]),
        }


#: Module-level singleton used across the plugin.
memory_store = MemoryStore()
