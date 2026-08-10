# -*- coding: utf-8 -*-
"""
GeoMind AI 云端服务 HTTP 客户端。

将网络交互从 QgsTask 中拆分出来，职责更单一：
- 只负责“提交任务 / 查询状态 / 下载结果 / 请求取消”这几个 HTTP 动作
- 不感知 QGIS / GDAL / 线程模型，方便未来单独测试或替换实现

打断机制说明：
requests 库发起的单次 HTTP 请求一旦发出无法从外部“硬中断”，
因此这里的取消策略是“在两次网络请求之间的空档尽快响应取消”，
配合 InterpretTask 中的高频 isCanceled() 检查，可以做到：
- 排队等待/轮询阶段：秒级响应取消
- 单次上传/下载请求进行中：该次请求完成后立刻停止，不再发起下一次请求
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
        # 登录后拿到的 JWT。账号网关校验的是这个 token，license_key/machine_id
        # 只作为兼容字段随请求带过去（网关会用用户名覆盖 machine_id 做统计）。
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
                     target_class: str = "", prompt: str = "") -> str:
        """提交解译任务，返回 task_id。"""
        from .constants import API_SUBMIT
        url = f"{self.server_url}{API_SUBMIT}"
        data = {
            'model_key': model_key,
            'license_key': self.license_key,
            'machine_id': self.machine_id,
        }
        if target_class:
            data['target_class'] = target_class
        if prompt:
            data['prompt'] = prompt

        with open(image_path, 'rb') as f:
            files = {'image': (os.path.basename(image_path), f, 'image/tiff')}
            resp = self._session.post(url, files=files, data=data, timeout=self.submit_timeout,
                                       headers=self._auth_headers())

        if resp.status_code == 402:
            # 账号网关：今日免费次数已用完
            raise RuntimeError(self._extract_error(resp))
        if resp.status_code == 401:
            raise RuntimeError("登录已过期，请重新登录后再试")
        if resp.status_code != 200:
            raise RuntimeError(f"提交任务失败 ({resp.status_code}): {self._extract_error(resp)}")

        return resp.json()["task_id"]

    # ------------------------------------------------------------ 查询状态 ---
    def get_status(self, task_id: str) -> dict:
        from .constants import API_STATUS
        url = f"{self.server_url}{API_STATUS.format(task_id=task_id)}"
        resp = self._session.get(url, timeout=self.request_timeout, headers=self._auth_headers())
        if resp.status_code != 200:
            raise RuntimeError(f"查询任务状态失败 ({resp.status_code}): {self._extract_error(resp)}")
        return resp.json()

    # ------------------------------------------------------------ 下载结果 ---
    def download_result(self, task_id: str, dest_path: str) -> None:
        from .constants import API_RESULT
        url = f"{self.server_url}{API_RESULT.format(task_id=task_id)}"
        resp = self._session.get(url, timeout=60, headers=self._auth_headers())
        if resp.status_code != 200:
            raise RuntimeError(f"下载结果文件失败 ({resp.status_code}): {self._extract_error(resp)}")
        with open(dest_path, 'wb') as f:
            f.write(resp.content)

    # ------------------------------------------------------ 取消任务(尽力而为) ---
    def cancel_task(self, task_id: str) -> None:
        """
        通知服务端取消任务。这是“尽力而为”的调用：
        如果服务端未实现取消接口，或网络异常，都会被静默忽略，
        因为本地任务无论如何都会立刻停止轮询/停止等待。
        """
        from .constants import API_CANCEL
        try:
            url = f"{self.server_url}{API_CANCEL.format(task_id=task_id)}"
            self._session.post(url, timeout=5, headers=self._auth_headers())
        except Exception:
            pass

    @staticmethod
    def _extract_error(resp) -> str:
        try:
            return resp.json().get('detail', resp.text)
        except Exception:
            return resp.text
