# -*- coding: utf-8 -*-
"""
生成稳定唯一的机器码
"""
import uuid
import platform
import hashlib


def get_machine_id() -> str:
    """结合网卡 MAC 地址和主机名生成唯一机器码"""
    try:
        mac = uuid.getnode()
        node = platform.node()
        raw_str = f"{mac}-{node}"
        md5_str = hashlib.md5(raw_str.encode('utf-8')).hexdigest().upper()
        # 格式化为 A1B2-C3D4-E5F6-7890
        return f"{md5_str[:4]}-{md5_str[4:8]}-{md5_str[8:12]}-{md5_str[12:16]}"
    except Exception:
        return "UNKNOWN-MACHINE-ID"