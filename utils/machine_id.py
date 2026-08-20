# -*- coding: utf-8 -*-
"""
Machine ID generation — produces a stable, unique hardware fingerprint.

Combines the MAC address and hostname into a SHA-256 hash, formatted
as a human-readable dash-separated string.
"""
import uuid
import platform
import hashlib


def get_machine_id() -> str:
    """Generate a stable machine ID from MAC address and hostname."""
    try:
        mac = uuid.getnode()
        node = platform.node()
        raw_str = f"{mac}-{node}"
        sha_str = hashlib.sha256(raw_str.encode("utf-8")).hexdigest().upper()
        return f"{sha_str[:4]}-{sha_str[4:8]}-{sha_str[8:12]}-{sha_str[12:16]}"
    except Exception:
        return "UNKNOWN-MACHINE-ID"
