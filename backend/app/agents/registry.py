"""智能体插件注册表。

内置适配器放在 ``app/agents/builtin``，首次使用时自动导入，
因此「新增一个文件」就是全部的接入步骤。
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable

from .base import AgentDefinition

_REGISTRY: dict[str, AgentDefinition] = {}
_loaded = False


def register_agent(cls: type[AgentDefinition]) -> type[AgentDefinition]:
    """每个适配器模块都会用到的类装饰器。"""
    if not getattr(cls, "id", None):
        raise ValueError(f"{cls.__name__} 必须声明 `id`")
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
        known = "、".join(sorted(_REGISTRY)) or "（无）"
        raise KeyError(f"未知的智能体类型「{agent_type_id}」（已注册：{known}）") from exc


def known_ids() -> Iterable[str]:
    _autoload()
    return sorted(_REGISTRY)
