# -*- coding: utf-8 -*-
from .constants import (
    API_REGISTER, API_LOGIN, API_USER_ME, API_PAYMENT_REDEEM,
)

# 修改密码接口路径
API_CHANGE_PASSWORD = "/api/v1/auth/change-password"


class AuthApiError(Exception):
    pass


class GeoMindAuthClient:

    def __init__(self, server_url: str, token: str = "", request_timeout: int = 15):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("缺少 requests 库，请执行 pip install requests") from exc

        self._requests = requests
        self.server_url = server_url.rstrip('/')
        self.token = token
        self.request_timeout = request_timeout

    def _headers(self):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _extract_error(resp) -> str:
        try:
            detail = resp.json().get('detail')
            return detail if detail else resp.text
        except Exception:
            return resp.text

    def _post(self, path: str, json_body: dict = None, auth: bool = False):
        url = f"{self.server_url}{path}"
        try:
            resp = self._requests.post(
                url, json=json_body, headers=self._headers() if auth else {},
                timeout=self.request_timeout,
            )
        except Exception as e:
            raise AuthApiError(f"无法连接服务器: {e}") from e
        if resp.status_code != 200:
            raise AuthApiError(self._extract_error(resp))
        return resp.json()

    def _get(self, path: str, auth: bool = True):
        url = f"{self.server_url}{path}"
        try:
            resp = self._requests.get(url, headers=self._headers() if auth else {},
                                       timeout=self.request_timeout)
        except Exception as e:
            raise AuthApiError(f"无法连接服务器: {e}") from e
        if resp.status_code != 200:
            raise AuthApiError(self._extract_error(resp))
        return resp.json()

    def register(self, username: str, password: str, machine_id: str) -> dict:
        """调用注册接口，附带机器码 machine_id"""
        return self._post(API_REGISTER, {
            "username": username,
            "password": password,
            "machine_id": machine_id
        })

    def login(self, username: str, password: str) -> dict:
        """调用登录接口"""
        return self._post(API_LOGIN, {"username": username, "password": password})

    def change_password(self, old_password: str, new_password: str) -> dict:
        """调用修改密码接口（需携带 Bearer Token 登录凭证）"""
        return self._post(
            API_CHANGE_PASSWORD,
            json_body={
                "old_password": old_password,
                "new_password": new_password
            },
            auth=True
        )

    def get_me(self) -> dict:
        """获取当前登录账号信息与套餐状态"""
        return self._get(API_USER_ME, auth=True)

    def redeem_card(self, code: str) -> dict:
        """调用卡密兑换接口"""
        return self._post(API_PAYMENT_REDEEM, json_body={"code": code}, auth=True)