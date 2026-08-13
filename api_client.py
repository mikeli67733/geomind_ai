# -*- coding: utf-8 -*-
"""
GeoMind AI 云端服务 HTTP 客户端。

将网络交互从 QgsTask 中拆分出来，职责更单一：
- 只负责“提交任务 / 查询状态 / 下载结果 / 请求取消”这几个 HTTP 动作
- 不感知 QGIS / GDAL / 线程模型，方便未来单独测试或替换实现

更新内容：
1. 增强 submit_task，支持双图 (image + image_after) 打包上传（用于变化检测）
2. 增强 submit_task，支持透传 output_format (mask 分割图斑 vs bbox 目标检测方框)
3. 增强 get_status / download_result，支持带上 model_key 帮助网关精准路由
"""

import os


class TaskCancelledError(Exception):
    """用户主动取消任务时抛出，用于从深层调用快速返回。"""
    pass


class GeoMindApiClient:

    def __init__(self, server_url: str, license_key: str, machine_id: str,
                 submit_timeout: int = 300, request_timeout: int = 30,
                 token: str = ""):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("缺少 requests 库，请执行 pip install requests") from exc

        self._requests = requests
        self._session = requests.Session()
        self.server_url = server_url.rstrip('/')
        self.license_key = license_key
        self.machine_id = machine_id
        self.submit_timeout = submit_timeout
        self.request_timeout = request_timeout
        # 登录后拿到的 JWT。账号网关校验的是这个 token
        self.token = token

    def _auth_headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 提交任务 ---
    def submit_task(self, image_path: str, model_key: str,
                    target_class: str = "", prompt: str = "",
                    image_after_path: str = None,
                    output_format: str = "mask") -> str:
        """
        提交解译任务，返回 task_id。
        支持单图提交 (image_path) 或双图提交 (image_path + image_after_path)
        支持 SAM3/目标检测输出模式 (output_format: 'mask' 或 'bbox')
        """
        from .constants import API_SUBMIT
        url = f"{self.server_url}{API_SUBMIT}"
        data = {
            'model_key': model_key,
            'license_key': self.license_key,
            'machine_id': self.machine_id,
            'output_format': output_format,  # 👈 新增：透传 SAM3 输出格式 ('mask' 或 'bbox')
        }
        if target_class:
            data['target_class'] = target_class
        if prompt:
            data['prompt'] = prompt

        files = {}
        f1 = open(image_path, 'rb')
        files['image'] = (os.path.basename(image_path), f1, 'image/tiff')

        f2 = None
        if image_after_path and os.path.exists(image_after_path):
            f2 = open(image_after_path, 'rb')
            files['image_after'] = (os.path.basename(image_after_path), f2, 'image/tiff')

        try:
            resp = self._session.post(url, files=files, data=data, timeout=self.submit_timeout,
                                       headers=self._auth_headers())
        finally:
            f1.close()
            if f2:
                f2.close()

        if resp.status_code == 402:
            # 账号网关：今日免费次数已用完
            raise RuntimeError(self._extract_error(resp))
        if resp.status_code == 401:
            raise RuntimeError("登录已过期，请重新登录后再试")
        if resp.status_code != 200:
            raise RuntimeError(f"提交任务失败 ({resp.status_code}): {self._extract_error(resp)}")

        return resp.json()["task_id"]

    # ------------------------------------------------------------ 查询状态 ---
    def get_status(self, task_id: str, model_key: str = "") -> dict:
        from .constants import API_STATUS
        url = f"{self.server_url}{API_STATUS.format(task_id=task_id)}"
        params = {'model_key': model_key} if model_key else {}
        resp = self._session.get(url, params=params, timeout=self.request_timeout, headers=self._auth_headers())
        if resp.status_code != 200:
            raise RuntimeError(f"查询任务状态失败 ({resp.status_code}): {self._extract_error(resp)}")
        return resp.json()

    # ------------------------------------------------------------ 下载结果 ---
    def download_result(self, task_id: str, dest_path: str, model_key: str = "") -> None:
        from .constants import API_RESULT
        url = f"{self.server_url}{API_RESULT.format(task_id=task_id)}"
        params = {'model_key': model_key} if model_key else {}
        resp = self._session.get(url, params=params, timeout=60, headers=self._auth_headers())
        if resp.status_code != 200:
            raise RuntimeError(f"下载结果文件失败 ({resp.status_code}): {self._extract_error(resp)}")
        with open(dest_path, 'wb') as f:
            f.write(resp.content)

    # ------------------------------------------------------ 取消任务(尽力而为) ---
    def cancel_task(self, task_id: str, model_key: str = "") -> None:
        """
        通知服务端取消任务。这是“尽力而为”的调用：
        如果服务端未实现取消接口，或网络异常，都会被静默忽略。
        """
        from .constants import API_CANCEL
        try:
            url = f"{self.server_url}{API_CANCEL.format(task_id=task_id)}"
            params = {'model_key': model_key} if model_key else {}
            self._session.post(url, params=params, timeout=5, headers=self._auth_headers())
        except Exception:
            pass

    @staticmethod
    def _extract_error(resp) -> str:
        try:
            return resp.json().get('detail', resp.text)
        except Exception:
            return resp.text