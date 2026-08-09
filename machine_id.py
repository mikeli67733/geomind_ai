# -*- coding: utf-8 -*-
"""
生成稳定唯一的机器码 (符合 QGIS 官方 Bandit 安全代码规范)
"""
import uuid
import platform
import hashlib


def get_machine_id() -> str:
    """结合网卡 MAC 地址和主机名生成唯一机器码 (使用 SHA256)"""
    try:
        mac = uuid.getnode()
        node = platform.node()
        raw_str = f"{mac}-{node}"
        # 使用符合 QGIS 官方 Bandit 安全规范的 SHA256 替代 MD5
        sha_str = hashlib.sha256(raw_str.encode('utf-8')).hexdigest().upper()
        # 格式化为 A1B2-C3D4-E5F6-7890
        return f"{sha_str[:4]}-{sha_str[4:8]}-{sha_str[8:12]}-{sha_str[12:16]}"
    except Exception:
        return "UNKNOWN-MACHINE-ID"