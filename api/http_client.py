# -*- coding: utf-8 -*-
"""
Unified HTTP transport for GeoMind AI API clients.

Centralizes: request timeouts, exponential-backoff retries on transient
network failures, bearer-token injection, extra header merging, and
normalization of transport-level errors into ``ServerUnreachableError``.

Business-level status codes (401/402/...) are intentionally left to the
caller so each client keeps its own semantic error mapping.
"""
import time
from typing import Optional

from ..core.exceptions import ServerUnreachableError
from ..core.logger import get_logger

logger = get_logger("api.http_client")

# Status codes that may succeed on retry at the transport level.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpClient:
    """Thin wrapper around ``requests.Session`` with retry + normalization."""

    def __init__(
        self,
        token: str = "",
        request_timeout: float = 15,
        retries: int = 2,
        backoff: float = 0.5,
    ):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("缺少 requests 库，请执行 pip install requests") from exc

        self._requests = requests
        self._session = requests.Session()
        self.token = token
        self.request_timeout = request_timeout
        self.retries = retries
        self.backoff = backoff

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
        auth: bool = False,
        retries: Optional[int] = None,
        retry_on_status: bool = False,
    ):
        """Perform a GET request; returns the ``requests.Response`` object."""
        return self.request(
            "GET", url,
            params=params, headers=headers, timeout=timeout,
            auth=auth, retries=retries, retry_on_status=retry_on_status,
        )

    def post(
        self,
        url: str,
        *,
        json: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
        auth: bool = False,
        retries: Optional[int] = None,
        retry_on_status: bool = False,
    ):
        """Perform a POST request; returns the ``requests.Response`` object."""
        return self.request(
            "POST", url,
            json=json, data=data, files=files, headers=headers, timeout=timeout,
            auth=auth, retries=retries, retry_on_status=retry_on_status,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
        auth: bool = False,
        retries: Optional[int] = None,
        retry_on_status: bool = False,
    ):
        """Core request loop with exponential-backoff retry on network errors."""
        max_retries = self.retries if retries is None else retries
        eff_timeout = self.request_timeout if timeout is None else timeout
        kwargs = {
            "params": params,
            "json": json,
            "data": data,
            "files": files,
            "timeout": eff_timeout,
        }

        for attempt in range(max_retries + 1):
            resp = self._attempt_once(method, url, headers, auth, kwargs, attempt, max_retries)
            if resp is None:
                continue  # transient failure, will retry (or raise on last attempt)
            if retry_on_status and resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
                logger.debug(
                    "HTTP %s %s -> %s (retry %d/%d)",
                    method, url, resp.status_code, attempt + 1, max_retries,
                )
                time.sleep(self.backoff * (2 ** attempt))
                continue
            return resp

        # Defensive — the loop always returns or raises above.
        raise ServerUnreachableError(f"请求失败: {method} {url}")

    def close(self) -> None:
        """Release the underlying connection pool."""
        try:
            self._session.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _headers(self, auth: bool = False) -> dict:
        headers = {"Accept": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _attempt_once(self, method, url, extra_headers, auth, kwargs, attempt, max_retries):
        headers = self._headers(auth)
        if extra_headers:
            headers.update(extra_headers)
        try:
            return self._session.request(method, url, headers=headers, **kwargs)
        except Exception as exc:
            if attempt < max_retries:
                logger.debug(
                    "HTTP %s %s failed (attempt %d/%d): %s",
                    method, url, attempt + 1, max_retries + 1, exc,
                )
                time.sleep(self.backoff * (2 ** attempt))
                return None
            raise ServerUnreachableError(f"无法连接服务器: {exc}") from exc
