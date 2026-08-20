# -*- coding: utf-8 -*-
"""
Authentication client for GeoMind AI.

Handles register, login, change-password, user info, and card redemption.
"""
from typing import Optional

from .http_client import HttpClient
from ..core.constants import (
    API_REGISTER,
    API_LOGIN,
    API_USER_ME,
    API_PAYMENT_REDEEM,
    API_CHANGE_PASSWORD,
)
from ..core.exceptions import AuthApiError, ServerUnreachableError
from ..core.logger import get_logger

logger = get_logger("api.auth_client")


class GeoMindAuthClient:
    """HTTP client for the GeoMind AI authentication gateway."""

    def __init__(self, server_url: str, token: str = "", request_timeout: int = 15):
        self._client = HttpClient(token=token, request_timeout=request_timeout)
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.request_timeout = request_timeout

    # -- Internal helpers ---------------------------------------------------

    @staticmethod
    def _extract_error(resp) -> str:
        try:
            detail = resp.json().get("detail")
            return detail if detail else resp.text
        except Exception:
            return resp.text

    def _post(self, path: str, json_body: Optional[dict] = None, auth: bool = False) -> dict:
        url = f"{self.server_url}{path}"
        try:
            resp = self._client.post(url, json=json_body, auth=auth)
        except ServerUnreachableError as e:
            raise AuthApiError(f"无法连接服务器: {e}") from e
        if resp.status_code != 200:
            raise AuthApiError(self._extract_error(resp))
        return resp.json()

    def _get(self, path: str, auth: bool = True) -> dict:
        url = f"{self.server_url}{path}"
        try:
            resp = self._client.get(url, auth=auth)
        except ServerUnreachableError as e:
            raise AuthApiError(f"无法连接服务器: {e}") from e
        if resp.status_code != 200:
            raise AuthApiError(self._extract_error(resp))
        return resp.json()

    # -- Public API ---------------------------------------------------------

    def register(self, username: str, password: str, machine_id: str) -> dict:
        """Register a new account with machine ID binding."""
        return self._post(
            API_REGISTER,
            {"username": username, "password": password, "machine_id": machine_id},
        )

    def login(self, username: str, password: str) -> dict:
        """Log in and receive a JWT access token."""
        return self._post(API_LOGIN, {"username": username, "password": password})

    def change_password(self, old_password: str, new_password: str) -> dict:
        """Change the current user's password (requires active token)."""
        return self._post(
            API_CHANGE_PASSWORD,
            json_body={"old_password": old_password, "new_password": new_password},
            auth=True,
        )

    def get_me(self) -> dict:
        """Fetch current account info and plan status."""
        return self._get(API_USER_ME, auth=True)

    def redeem_card(self, code: str) -> dict:
        """Redeem a card-key to upgrade plan."""
        return self._post(API_PAYMENT_REDEEM, json_body={"code": code}, auth=True)
