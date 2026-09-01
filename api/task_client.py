# -*- coding: utf-8 -*-
"""
GeoMind AI cloud service HTTP client.

Responsibilities:
    - Submit interpretation tasks (single or dual image)
    - Poll task status
    - Download result files
    - Request task cancellation

This module is intentionally unaware of QGIS / GDAL / threading so it
can be unit-tested in isolation.
"""
import os
from typing import Optional

from .http_client import HttpClient
from ..core.logger import get_logger
from ..core.exceptions import GeoMindError, TaskCancelledError, QuotaExhaustedError, TokenExpiredError

logger = get_logger("api.task_client")


class GeoMindApiClient:
    """HTTP client for the GeoMind AI task gateway."""

    def __init__(
        self,
        server_url: str,
        license_key: str,
        machine_id: str,
        submit_timeout: int = 300,
        request_timeout: int = 30,
        token: str = "",
    ):
        self._client = HttpClient(token=token, request_timeout=request_timeout)
        self.server_url = server_url.rstrip("/")
        self.license_key = license_key
        self.machine_id = machine_id
        self.submit_timeout = submit_timeout
        self.request_timeout = request_timeout
        self.token = token

    # -- Internal helpers ---------------------------------------------------

    @staticmethod
    def _extract_error(resp) -> str:
        try:
            return resp.json().get("detail", resp.text)
        except Exception:
            return resp.text

    def _check_response(self, resp, action: str):
        """Raise domain-specific exceptions for known HTTP error codes."""
        if resp.status_code == 401:
            raise TokenExpiredError("登录已过期，请重新登录后再试")
        if resp.status_code == 402:
            raise QuotaExhaustedError(self._extract_error(resp))
        if resp.status_code != 200:
            raise GeoMindError(
                f"{action}失败 ({resp.status_code}): {self._extract_error(resp)}"
            )

    def close(self):
        self._client.close()

    # -- Public API ---------------------------------------------------------

    def submit_task(
        self,
        image_path: str,
        model_key: str,
        target_class: str = "",
        prompt: str = "",
        image_after_path: Optional[str] = None,
        output_format: str = "mask",
    ) -> str:
        """Submit an interpretation task and return the task_id."""
        from ..core.constants import API_SUBMIT

        url = f"{self.server_url}{API_SUBMIT}"
        data = {
            "model_key": model_key,
            "license_key": self.license_key,
            "machine_id": self.machine_id,
            "output_format": output_format,
        }
        if target_class:
            data["target_class"] = target_class
        if prompt:
            data["prompt"] = prompt

        files = {}
        f1 = open(image_path, "rb")
        files["image"] = (os.path.basename(image_path), f1, "image/tiff")

        f2 = None
        if image_after_path and os.path.exists(image_after_path):
            f2 = open(image_after_path, "rb")
            files["image_after"] = (os.path.basename(image_after_path), f2, "image/tiff")

        try:
            resp = self._client.post(
                url,
                files=files,
                data=data,
                timeout=self.submit_timeout,
                auth=True,
            )
        finally:
            f1.close()
            if f2:
                f2.close()

        self._check_response(resp, "提交任务")
        return resp.json()["task_id"]

    def get_status(self, task_id: str, model_key: str = "") -> dict:
        """Query the current status of a task."""
        from ..core.constants import API_STATUS

        url = f"{self.server_url}{API_STATUS.format(task_id=task_id)}"
        params = {"model_key": model_key} if model_key else {}
        resp = self._client.get(
            url, params=params, timeout=self.request_timeout, auth=True,
            retry_on_status=True,  # 瞬时 5xx 自动重试，避免长任务被一次抖动打断
        )
        self._check_response(resp, "查询任务状态")
        return resp.json()

    def download_result(self, task_id: str, dest_path: str, model_key: str = "") -> None:
        """Download the result file for a completed task."""
        from ..core.constants import API_RESULT

        url = f"{self.server_url}{API_RESULT.format(task_id=task_id)}"
        params = {"model_key": model_key} if model_key else {}
        resp = self._client.get(
            url, params=params, timeout=180, auth=True,  # 结果文件可能较大，放宽超时
            retry_on_status=True,
        )
        self._check_response(resp, "下载结果文件")
        with open(dest_path, "wb") as f:
            f.write(resp.content)

    def cancel_task(self, task_id: str, model_key: str = "") -> None:
        """Best-effort task cancellation — errors are silently ignored."""
        from ..core.constants import API_CANCEL

        try:
            url = f"{self.server_url}{API_CANCEL.format(task_id=task_id)}"
            params = {"model_key": model_key} if model_key else {}
            self._client.post(
                url, params=params, timeout=5, auth=True
            )
        except Exception as e:
            logger.debug("Cancel request failed (ignored): %s", e)
