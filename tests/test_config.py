# -*- coding: utf-8 -*-
"""Unit tests for core/config.py — lazy, layered settings resolution."""
import geomind_ai.core.config as cfg
from geomind_ai.core.config import Settings
from geomind_ai.core.constants import FALLBACK_SERVER_URL


def test_instantiating_settings_never_fetches():
    # The whole point of v5.0: constructing settings (and importing the
    # package) must not trigger any network call.
    s = Settings(plugin_dir="/definitely/not/exist")
    assert s._remote_fetched is False


def test_local_file_wins_over_remote(tmp_path, monkeypatch):
    (tmp_path / "server_config.json").write_text(
        '{"server_url": "http://local:9000"}', encoding="utf-8"
    )
    monkeypatch.setattr(cfg, "_fetch_remote_server_url", lambda: "http://remote:8000")
    s = Settings(plugin_dir=str(tmp_path))
    assert s.server_url() == "http://local:9000"


def test_remote_fetch_when_no_local(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "_fetch_remote_server_url", lambda: "http://remote:8000")
    s = Settings(plugin_dir=str(tmp_path))
    assert s.server_url() == "http://remote:8000"


def test_fallback_when_all_sources_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "_fetch_remote_server_url", lambda: None)
    s = Settings(plugin_dir=str(tmp_path))
    assert s.server_url() == FALLBACK_SERVER_URL


def test_remote_result_is_cached(tmp_path, monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return "http://remote:8000"

    monkeypatch.setattr(cfg, "_fetch_remote_server_url", fake)
    s = Settings(plugin_dir=str(tmp_path))
    assert s.server_url() == "http://remote:8000"
    assert s.server_url() == "http://remote:8000"
    assert len(calls) == 1  # second call served from cache
    s.server_url(force_refresh=True)
    assert len(calls) == 2  # explicit refresh re-fetches


def test_corrupt_local_file_is_ignored(tmp_path, monkeypatch):
    (tmp_path / "server_config.json").write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(cfg, "_fetch_remote_server_url", lambda: None)
    s = Settings(plugin_dir=str(tmp_path))
    assert s.server_url() == FALLBACK_SERVER_URL
