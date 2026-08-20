# -*- coding: utf-8 -*-
"""
Custom exception hierarchy for GeoMind AI.

All domain-specific exceptions inherit from GeoMindError so callers can
catch plugin errors with a single ``except GeoMindError`` clause.
"""


class GeoMindError(Exception):
    """Base exception for all GeoMind AI errors."""


class TaskCancelledError(GeoMindError):
    """Raised when a user cancels an ongoing interpretation task."""
    pass


class AuthApiError(GeoMindError):
    """Raised when an authentication API call fails."""
    pass


class QuotaExhaustedError(GeoMindError):
    """Raised when the daily free quota is exhausted."""
    pass


class TokenExpiredError(GeoMindError):
    """Raised when the JWT token has expired or is invalid."""
    pass


class ServerUnreachableError(GeoMindError):
    """Raised when the remote server cannot be reached."""
    pass


class RasterProcessingError(GeoMindError):
    """Raised when a raster processing operation fails."""
    pass


class ExtentTooLargeError(GeoMindError):
    """Raised when the selected interpretation extent exceeds safe limits."""
    pass
