# -*- coding: utf-8 -*-
"""
插件常量与配置集中定义模块。
把原本散落在 dockwidget.py / worker.py 中的“魔法字符串”统一收敛到这里，
方便以后新增模型、调整接口路径时只改一处。
"""

# ---------------------------------------------------------------- 模型配置 ---
# (显示名称, model_key, UI 模式)
MODELS = [
    ("土地利用/多要素识别 (LANDUSE)", "LANDUSE", "landuse"),
    ("SAM3 提示词通用大模型 (SAM3)", "SAM3_MODEL", "sam3"),
]

# 土地利用模型对应的类别映射 (显示名称, 类别 ID)
LANDUSE_CLASSES = [
    ("耕地 ", 1),
    ("林地 ", 3),
    ("草地 ", 4),
    ("建筑 ", 5),
    ("道路 ", 6),
    ("施工 ", 7),
    ("水体 ", 10),
]

# 默认勾选的类别 ID
DEFAULT_CHECKED_CLASS_IDS = {10}

# --------------------------------------------------------------- 接口路径 ---
API_SUBMIT = "/api/v1/task/submit"
API_STATUS = "/api/v1/task/status/{task_id}"
API_RESULT = "/api/v1/task/result/{task_id}"
# 取消接口为“尽力而为”调用：如果后端未实现该路由，客户端会静默忽略错误，
# 不影响本地任务的中断流程。
API_CANCEL = "/api/v1/task/cancel/{task_id}"

# --------------------------------------------------------------- 默认设置 ---
DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
DEFAULT_POLL_INTERVAL = 2.0      # 秒，正常轮询间隔
CANCEL_CHECK_INTERVAL = 0.3      # 秒，等待轮询期间检查“是否已取消”的粒度
DEFAULT_TIMEOUT = 600            # 秒，整体任务超时时间

SETTINGS_ORG = "ImageInterpretPlugin"
SETTINGS_APP = "ImageInterpretPlugin"
SETTINGS_KEY_SERVER_URL = "server_url"
SETTINGS_KEY_LICENSE_KEY = "license_key"

PLUGIN_TASK_DESCRIPTION = "GeoMind AI 遥感影像智能解译"
