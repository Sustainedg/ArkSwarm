"""Agent plugin registry.

Built-in adapters live in ``app/agents/builtin`` and are auto-imported on first
use, so adding a file there is the entire integration step.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable

from .base import AgentDefinition

_REGISTRY: dict[str, AgentDefinition] = {}
_loaded = False


def register_agent(cls: type[AgentDefinition]) -> type[AgentDefinition]:
    """Class decorator used by every adapter module."""
    if not getattr(cls, "id", None):
        raise ValueError(f"{cls.__name__} must declare an `id`")
    _REGISTRY[cls.id] = cls()
    return cls


def _autoload() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import builtin

    for module in pkgutil.iter_modules(builtin.__path__):
        importlib.import_module(f"{builtin.__name__}.{module.name}")


def all_agents() -> list[AgentDefinition]:
    _autoload()
    return sorted(_REGISTRY.values(), key=lambda a: (a.vendor, a.name))


def get_agent(agent_type_id: str) -> AgentDefinition:
    _autoload()
    try:
        return _REGISTRY[agent_type_id]
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(f"unknown agent type '{agent_type_id}' (known: {known})") from exc


def known_ids() -> Iterable[str]:
    _autoload()
    return sorted(_REGISTRY)
