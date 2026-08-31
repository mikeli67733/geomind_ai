# -*- coding: utf-8 -*-
"""
History store — per-page run records with folder-level backtracking.

Every page of the plugin (local tools, cloud AI tasks, Copilot chat) keeps
its own folder under ``history/<page_key>/``; each run/session creates a
timestamped sub-folder containing:

    record.json   metadata + params + inputs + outputs + status/duration
    params.json   parameter snapshot (used to re-run with pre-filled params)
    log.txt       human-readable run log
    outputs/      small result files copied here (< COPY_OUTPUT_MAX_BYTES)

Result files larger than the copy threshold are only recorded by path so
the history never silently eats the user's disk.  The base directory is
the QGIS user profile dir (``geomind_ai/``), writable and persistent.
"""
import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from .logger import get_logger

logger = get_logger("core.history")

#: Result files up to this size are copied into the run folder for full
#: backtracking; larger files are recorded by path only.
COPY_OUTPUT_MAX_BYTES = 20 * 1024 * 1024

RECORD_FILE = "record.json"
PARAMS_FILE = "params.json"
LOG_FILE = "log.txt"
OUTPUTS_DIR = "outputs"


def _data_root() -> str:
    """Resolve the per-user GeoMind data directory inside the QGIS profile."""
    from qgis.core import QgsApplication
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "geomind_ai")


def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _unique_stamp(base_dir: str) -> str:
    """Timestamp that never collides with an existing folder."""
    stamp = _now_stamp()
    n = 1
    candidate = stamp
    while os.path.isdir(os.path.join(base_dir, candidate)):
        n += 1
        candidate = f"{stamp}_{n}"
    return candidate


def _json_safe(value: Any) -> Any:
    """Convert a value into something json.dumps can serialize."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    # QGIS layer / extent objects — record a lightweight fingerprint.
    if hasattr(value, "id") and hasattr(value, "name") and hasattr(value, "source"):
        return {
            "_type": "layer",
            "id": str(value.id()),
            "name": str(value.name()),
            "source": str(value.source()),
        }
    if hasattr(value, "asWktPolygon"):
        try:
            return {"_type": "extent", "wkt": value.asWktPolygon()}
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return repr(value)


class HistoryStore:
    """File-backed history records, one folder per page, one sub-folder per run."""

    def __init__(self, base_dir: Optional[str] = None):
        # The QGIS profile dir is resolved lazily so importing this module
        # never requires a running QGIS (same pattern as core.config).
        self._base_dir = base_dir
        if base_dir is not None:
            os.makedirs(base_dir, exist_ok=True)

    @property
    def base_dir(self) -> str:
        if self._base_dir is None:
            self._base_dir = _data_root()
            os.makedirs(self._base_dir, exist_ok=True)
        return self._base_dir

    @property
    def history_dir(self) -> str:
        path = os.path.join(self.base_dir, "history")
        os.makedirs(path, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Run records (local tools / cloud AI tasks)
    # ------------------------------------------------------------------
    def record_run(
        self,
        page_key: str,
        page_title: str,
        status: str,  # "ok" | "failed" | "cancelled"
        params: Optional[dict] = None,
        summary: str = "",
        input_layers: Optional[list] = None,
        output_files: Optional[list] = None,
        log_text: str = "",
        duration_ms: int = 0,
        error: str = "",
        extra: Optional[dict] = None,
    ) -> str:
        """Persist one run and return its folder path."""
        page_dir = os.path.join(self.history_dir, page_key)
        os.makedirs(page_dir, exist_ok=True)
        run_dir = os.path.join(page_dir, _unique_stamp(page_dir))
        os.makedirs(run_dir, exist_ok=True)

        params_safe = _json_safe(params or {})
        inputs_safe = _json_safe(input_layers or [])
        outputs_safe = []
        for out_path in output_files or []:
            copied = self._copy_output(run_dir, out_path)
            outputs_safe.append({"path": str(out_path), "copied": copied})

        record = {
            "page_key": page_key,
            "page_title": page_title,
            "status": status,
            "summary": summary,
            "error": error,
            "params": params_safe,
            "input_layers": inputs_safe,
            "outputs": outputs_safe,
            "duration_ms": duration_ms,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "extra": extra or {},
        }
        self._write_json(os.path.join(run_dir, RECORD_FILE), record)
        self._write_json(os.path.join(run_dir, PARAMS_FILE), params_safe)

        log_lines = [
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 页面: {page_title} ({page_key})",
            f"状态: {status}",
            f"耗时: {duration_ms} ms",
        ]
        if summary:
            log_lines.append(f"摘要: {summary}")
        if error:
            log_lines.append(f"错误: {error}")
        if log_text:
            log_lines.append("")
            log_lines.append(str(log_text))
        with open(os.path.join(run_dir, LOG_FILE), "w", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines))

        logger.info("History recorded: %s", run_dir)
        return run_dir

    def _copy_output(self, run_dir: str, path: str) -> str:
        """Copy a small result file into outputs/; returns the copied path or ''."""
        if not path or not os.path.isfile(path):
            return ""
        try:
            if os.path.getsize(path) > COPY_OUTPUT_MAX_BYTES:
                return ""
            out_dir = os.path.join(run_dir, OUTPUTS_DIR)
            os.makedirs(out_dir, exist_ok=True)
            dst = os.path.join(out_dir, os.path.basename(path))
            if os.path.abspath(dst) != os.path.abspath(path):
                shutil.copy2(path, dst)
            return dst
        except OSError as exc:
            logger.debug("Failed to copy output %s: %s", path, exc)
            return ""

    # ------------------------------------------------------------------
    # Copilot chat sessions
    # ------------------------------------------------------------------
    def new_session_dir(self, page_key: str = "copilot") -> str:
        """Create a fresh session folder and return its path."""
        page_dir = os.path.join(self.history_dir, page_key)
        os.makedirs(page_dir, exist_ok=True)
        session_dir = os.path.join(page_dir, _unique_stamp(page_dir))
        os.makedirs(session_dir, exist_ok=True)
        self._write_json(os.path.join(session_dir, "session.json"), {
            "page_key": page_key,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "message_count": 0,
        })
        return session_dir

    def append_message(self, session_dir: str, message: dict) -> None:
        """Append one chat message (jsonl) and update the session count."""
        if not session_dir:
            return
        try:
            with open(os.path.join(session_dir, "chat.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(_json_safe(message), ensure_ascii=False) + "\n")
            self._bump_session_count(session_dir)
        except OSError as exc:
            logger.debug("Failed to append chat message: %s", exc)

    def finalize_session(self, session_dir: str) -> None:
        """Mark a session as closed (end time + message count)."""
        if not session_dir or not os.path.isdir(session_dir):
            return
        meta_path = os.path.join(session_dir, "session.json")
        meta = self._read_json(meta_path) or {}
        meta["ended_at"] = datetime.now().isoformat(timespec="seconds")
        meta["message_count"] = self._count_messages(session_dir)
        self._write_json(meta_path, meta)

    def _bump_session_count(self, session_dir: str) -> None:
        meta_path = os.path.join(session_dir, "session.json")
        meta = self._read_json(meta_path) or {}
        meta["message_count"] = self._count_messages(session_dir)
        self._write_json(meta_path, meta)

    def _count_messages(self, session_dir: str) -> int:
        try:
            with open(os.path.join(session_dir, "chat.jsonl"), encoding="utf-8") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    # ------------------------------------------------------------------
    # Listing / inspection
    # ------------------------------------------------------------------
    def list_records(
        self,
        page_key: Optional[str] = None,
        keyword: str = "",
        limit: int = 300,
    ) -> List[dict]:
        """Return run records sorted newest-first. Corrupt records are skipped."""
        records: List[dict] = []
        page_dirs = [page_key] if page_key else self._page_keys()
        for pk in page_dirs:
            page_dir = os.path.join(self.history_dir, pk)
            if not os.path.isdir(page_dir):
                continue
            for run_name in os.listdir(page_dir):
                record_path = os.path.join(page_dir, run_name, RECORD_FILE)
                if not os.path.isfile(record_path):
                    continue
                record = self._read_json(record_path)
                if not record:
                    continue
                record["folder"] = os.path.join(page_dir, run_name)
                records.append(record)
        records.sort(key=lambda r: (r.get("created_at", ""), r.get("folder", "")), reverse=True)
        if keyword:
            kw = keyword.lower()
            records = [
                r for r in records
                if kw in str(r.get("summary", "")).lower()
                or kw in str(r.get("page_title", "")).lower()
                or kw in json.dumps(r.get("params", {}), ensure_ascii=False).lower()
            ]
        return records[:limit]

    def list_copilot_sessions(self, limit: int = 100) -> List[dict]:
        """Return Copilot session summaries, newest-first."""
        sessions: List[dict] = []
        page_dir = os.path.join(self.history_dir, "copilot")
        if not os.path.isdir(page_dir):
            return sessions
        for name in os.listdir(page_dir):
            meta_path = os.path.join(page_dir, name, "session.json")
            if not os.path.isfile(meta_path):
                continue
            meta = self._read_json(meta_path)
            if not meta:
                continue
            meta["folder"] = os.path.join(page_dir, name)
            meta["name"] = name
            sessions.append(meta)
        sessions.sort(key=lambda s: (s.get("created_at", ""), s.get("name", "")), reverse=True)
        return sessions[:limit]

    def read_session_chat(self, session_dir: str, limit: int = 500) -> List[dict]:
        """Read the messages of one Copilot session."""
        messages: List[dict] = []
        path = os.path.join(session_dir, "chat.jsonl")
        if not os.path.isfile(path):
            return messages
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(messages) >= limit:
                    break
        return messages

    def get_record(self, folder: str) -> Optional[dict]:
        """Load a single record by its folder path."""
        record = self._read_json(os.path.join(folder, RECORD_FILE))
        if record:
            record["folder"] = folder
        return record

    def read_log(self, folder: str) -> str:
        try:
            with open(os.path.join(folder, LOG_FILE), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def load_params(self, folder: str) -> dict:
        return self._read_json(os.path.join(folder, PARAMS_FILE)) or {}

    def delete_record(self, folder: str) -> None:
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)

    def clear_page(self, page_key: str) -> None:
        page_dir = os.path.join(self.history_dir, page_key)
        if os.path.isdir(page_dir):
            shutil.rmtree(page_dir, ignore_errors=True)
            os.makedirs(page_dir, exist_ok=True)

    def clear_all(self) -> None:
        if os.path.isdir(self.history_dir):
            shutil.rmtree(self.history_dir, ignore_errors=True)
        os.makedirs(self.history_dir, exist_ok=True)

    def page_stats(self) -> Dict[str, int]:
        """Count records per page key."""
        counts: Dict[str, int] = {}
        for pk in self._page_keys():
            page_dir = os.path.join(self.history_dir, pk)
            if not os.path.isdir(page_dir):
                continue
            counts[pk] = sum(
                1 for name in os.listdir(page_dir)
                if os.path.isfile(os.path.join(page_dir, name, RECORD_FILE))
            )
        return counts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _page_keys(self) -> List[str]:
        try:
            return sorted(
                name for name in os.listdir(self.history_dir)
                if os.path.isdir(os.path.join(self.history_dir, name))
            )
        except OSError:
            return []

    @staticmethod
    def _write_json(path: str, data: dict) -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.debug("Failed to write %s: %s", path, exc)

    @staticmethod
    def _read_json(path: str) -> Optional[dict]:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def open_folder(self, folder: str) -> None:
        """Reveal a history folder in the OS file manager."""
        if not os.path.isdir(folder):
            return
        try:
            from qgis.PyQt.QtCore import QUrl
            from qgis.PyQt.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        except Exception as exc:
            logger.debug("Failed to open folder %s: %s", folder, exc)


#: Module-level singleton used across the plugin.
history_store = HistoryStore()
