# -*- coding: utf-8 -*-
"""Unit tests for core/exceptions.py — domain exception hierarchy."""
import pytest

from geomind_ai.core.exceptions import (
    GeoMindError,
    TaskCancelledError,
    AuthApiError,
    QuotaExhaustedError,
    TokenExpiredError,
    ServerUnreachableError,
    RasterProcessingError,
)

_ALL_DERIVED = (
    TaskCancelledError,
    AuthApiError,
    QuotaExhaustedError,
    TokenExpiredError,
    ServerUnreachableError,
    RasterProcessingError,
)


def test_all_domain_errors_derive_from_base():
    for cls in _ALL_DERIVED:
        assert issubclass(cls, GeoMindError)


def test_catch_base_catches_any_domain_error():
    for cls in _ALL_DERIVED:
        with pytest.raises(GeoMindError):
            raise cls("boom")


def test_messages_are_preserved():
    with pytest.raises(AuthApiError, match="bad credentials"):
        raise AuthApiError("bad credentials")
