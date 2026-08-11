# -*- coding: utf-8 -*-
"""
插件常量与配置集中定义模块。
"""
# constants.py
import time
import requests

# 配置源列表：优先使用国内能直连的加速代理，备用 GitHub 官方 Raw
REMOTE_CONFIG_URLS = [
    # 方案 B：GitHub 官方 Raw 直链
    "https://api.npoint.io/73f56594195f39efc03d",
]

# FALLBACK_SERVER_URL = "https://application-showed-revolutionary-flooring.trycloudflare.com"


def fetch_remote_server_url() -> str:
    """动态拉取远程服务地址（支持多源容错与国内加速）"""
    timestamp = int(time.time())

    for url in REMOTE_CONFIG_URLS:
        try:
            # 加上时间戳，防止本地 HTTP 缓存
            cache_buster_url = f"{url}?_t={timestamp}"
            resp = requests.get(cache_buster_url, timeout=4)
            resp.raise_for_status()

            remote_cfg = resp.json()
            print(remote_cfg)

            server_url = remote_cfg.get("server_url")

            if server_url:
                return server_url
        except Exception as e:
            # 单个源失败时打印日志，并自动尝试下一个源
            print(f"[GeoMind] 从 {url} 获取配置失败: {e}")
            continue

    # # 所有远程源都拉取失败时，回退兜底地址
    # print("[GeoMind] 所有远程配置源均获取失败，回退默认地址")
    # return FALLBACK_SERVER_URL


DEFAULT_SERVER_URL = fetch_remote_server_url()
print(DEFAULT_SERVER_URL)

MODELS = [
    ("土地利用/多要素识别 (LANDUSE)", "LANDUSE", "landuse"),
    ("SAM3 提示词通用大模型 (SAM3)", "SAM3_MODEL", "sam3"),
]

LANDUSE_CLASSES = [
    ("耕地 ", 1),
    ("林地 ", 3),
    ("草地 ", 4),
    ("建筑 ", 5),
    ("道路 ", 6),
    ("施工 ", 7),
    ("水体 ", 10),
]

DEFAULT_CHECKED_CLASS_IDS = {10}

# --------------------------------------------------------------- 接口路径 ---
API_SUBMIT = "/api/v1/task/submit"
API_STATUS = "/api/v1/task/status/{task_id}"
API_RESULT = "/api/v1/task/result/{task_id}"
API_CANCEL = "/api/v1/task/cancel/{task_id}"

# 账号 / 卡密兑换相关接口
API_REGISTER = "/api/v1/auth/register"
API_LOGIN = "/api/v1/auth/login"
API_USER_ME = "/api/v1/user/me"
API_PAYMENT_REDEEM = "/api/v1/payment/redeem"

# ----------------------------------------------------------- 闲鱼商品链接 ---
# 替换为你在闲鱼生成的商品链接或店铺链接（支持网页版/淘口令解析出来的短链）
XIANYU_PRODUCT_URL = "https://m.tb.cn/h.8SfKfsd?tk=u3XFgAMYbS2"

# --------------------------------------------------------------- 默认设置 ---
DEFAULT_POLL_INTERVAL = 2.0
CANCEL_CHECK_INTERVAL = 0.3
DEFAULT_TIMEOUT = 600

SETTINGS_ORG = "ImageInterpretPlugin"
SETTINGS_APP = "ImageInterpretPlugin"
SETTINGS_KEY_SERVER_URL = "server_url"
SETTINGS_KEY_LICENSE_KEY = "license_key"
SETTINGS_KEY_TOKEN = "auth_token"
SETTINGS_KEY_USERNAME = "auth_username"

PLUGIN_TASK_DESCRIPTION = "GeoMind AI 遥感影像智能解译"

FREE_PLAN_DAILY_QUOTA = 20
PRO_PLAN_PRICE_YUAN = 99
PRO_PLAN_DAYS = 30
CUSTOM_PLAN_CONTACT_TEXT = "定制服务 / 私有化部署按解译面积报价，请扫码联系作者"

PLAN_LABELS = {
    "free": "免费版",
    "pro": "包月会员 (PRO)",
    "custom": "定制版/私有化部署",
}