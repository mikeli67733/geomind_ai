# -*- coding: utf-8 -*-
"""
Session context — a single snapshot of per-user runtime credentials.

UI layers (dock/copilot widgets) previously reached into each other's
attributes (``self.dock.token`` etc.).  Centralizing them here keeps
business code decoupled from widget internals and makes threading the
values into skills/tasks explicit.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContext:
    """Immutable snapshot of server connection + identity."""

    server_url: str = ""
    token: str = ""
    machine_id: str = ""

    def is_authenticated(self) -> bool:
        return bool(self.token)
