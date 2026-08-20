# -*- coding: utf-8 -*-
"""Unit tests for api/http_client.py — retry, auth, error normalization."""
from unittest.mock import Mock

import pytest

from geomind_ai.api.http_client import HttpClient
from geomind_ai.core.exceptions import ServerUnreachableError


def _client(**kwargs):
    kwargs.setdefault("retries", 1)
    kwargs.setdefault("backoff", 0)
    client = HttpClient(**kwargs)
    client._session = Mock()
    return client


def _resp(status=200, text="ok"):
    resp = Mock(status_code=status)
    resp.text = text
    return resp


def test_auth_token_injected():
    client = _client(token="tok123")
    client._session.request.return_value = _resp()
    client.get("http://x/api", auth=True)
    headers = client._session.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok123"


def test_no_token_header_when_auth_false():
    client = _client(token="tok123")
    client._session.request.return_value = _resp()
    client.get("http://x/api", auth=False)
    headers = client._session.request.call_args.kwargs["headers"]
    assert "Authorization" not in headers


def test_extra_headers_merged():
    client = _client(token="tok123")
    client._session.request.return_value = _resp()
    client.get("http://x/api", auth=True, headers={"User-Agent": "t"})
    headers = client._session.request.call_args.kwargs["headers"]
    assert headers["User-Agent"] == "t"
    assert headers["Authorization"] == "Bearer tok123"


def test_transport_error_raises_server_unreachable():
    client = _client(retries=1)
    client._session.request.side_effect = ConnectionError("boom")
    with pytest.raises(ServerUnreachableError):
        client.get("http://x/api")


def test_retries_on_transient_network_error():
    client = _client(retries=2)
    client._session.request.side_effect = [ConnectionError("a"), _resp()]
    resp = client.get("http://x/api")
    assert client._session.request.call_count == 2
    assert resp.status_code == 200


def test_no_status_retry_by_default():
    client = _client(retries=2)
    client._session.request.return_value = _resp(status=500)
    resp = client.get("http://x/api")
    assert client._session.request.call_count == 1
    assert resp.status_code == 500


def test_retry_on_retryable_status():
    client = _client(retries=2)
    client._session.request.side_effect = [_resp(status=502), _resp(status=200)]
    resp = client.get("http://x/api", retry_on_status=True)
    assert client._session.request.call_count == 2
    assert resp.status_code == 200


def test_post_passes_method_and_json():
    client = _client()
    client._session.request.return_value = _resp()
    client.post("http://x/api", json={"a": 1})
    call = client._session.request.call_args
    assert call.args[0] == "POST"
    assert call.kwargs["json"] == {"a": 1}


def test_default_timeout_applied():
    client = _client()
    client._session.request.return_value = _resp()
    client.get("http://x/api")
    assert client._session.request.call_args.kwargs["timeout"] == client.request_timeout


def test_explicit_timeout_overrides():
    client = _client()
    client._session.request.return_value = _resp()
    client.get("http://x/api", timeout=7.5)
    assert client._session.request.call_args.kwargs["timeout"] == 7.5
