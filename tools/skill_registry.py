# -*- coding: utf-8 -*-
"""
Explicit skill whitelist registry.

The Copilot loop used to resolve tool names via ``getattr`` on the whole
dispatcher module, which allowed the LLM to reach any callable (including
private helpers). Skills must now be registered here to be executable;
``tools.skills`` populates this registry at import time.

Each entry maps an LLM-facing function name to its implementation, a
human-friendly label and a dispatch kind used by the UI layer.
"""
from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass(frozen=True)
class SkillSpec:
    """One whitelisted LLM-callable skill."""

    name: str
    func: Callable
    label: str
    kind: str  # "local" | "ai" | "qgis" | "web"


_SKILLS: Dict[str, SkillSpec] = {}


def register(name: str, func: Callable, label: str = "", kind: str = "local") -> None:
    """Whitelist one skill. Later registrations with the same name win."""
    _SKILLS[name] = SkillSpec(name=name, func=func, label=label or name, kind=kind)


def get_skill(name: str) -> Optional[SkillSpec]:
    """Return the spec for *name*, or None if not whitelisted."""
    return _SKILLS.get(name)


def skill_label(name: str) -> str:
    """Human-readable label for progress cards; falls back to the raw name."""
    spec = _SKILLS.get(name)
    return spec.label if spec else name


def all_skills() -> Dict[str, SkillSpec]:
    """Snapshot of every registered skill."""
    return dict(_SKILLS)
