"""内核隔离配置目录：技能与 MCP 共用同一个「家」。

「严格仅分配」的前提是把内核指向一个平台自己维护的配置目录：里面只放分配到的技能与
MCP 服务器声明，内核读不到本机个人配置里的那一份。目录按「对话」切分，放在数据目录里
（``<data>/agent-homes/<对话 id>/<home_name>``）—— 内核自己的会话记录也写在这里，
每次运行都换目录会让多轮对话断掉。

本模块只做「目录怎么算、怎么建、怎么提示」这三件事；「哪个内核的家叫什么名字」仍然是
适配器的知识（``SkillMount.home_name`` / ``McpMount.home_name``）。
"""
from __future__ import annotations

import os
from pathlib import Path

from ..agents.base import AgentContext
from ..settings import get_settings


def home_for(scope_id: str, home_name: str) -> Path:
    """某个作用域（对话 id 或协作运行 id）下一次运行的内核隔离目录。"""
    return get_settings().agent_homes_root / scope_id / home_name


def ensure_home(home: Path) -> list[str]:
    """建出隔离目录并收紧权限；返回面向用户的告警（失败不抛错）。"""
    try:
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
    except OSError as exc:
        return [f"无法创建内核隔离目录 {home}：{exc}"]
    return []


def note_fresh_home(ctx: AgentContext, *, first_run: bool) -> list[str]:
    """隔离目录是刚建的时候，续接旧会话必然失败：改成本轮新会话并明确告知用户。"""
    if not first_run or not ctx.session_id:
        return []
    ctx.session_id = None
    return [
        "内核隔离目录里还没有这条对话的历史会话，本轮已改为新会话"
        "（旧会话记录仍在原配置目录中，如需保留请到「对话」页操作）"
    ]


def safe_relative(relative: str) -> Path | None:
    """把「相对路径」限制成不会越出目录的安全路径；不合法时返回 ``None``。"""
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts:
        return None
    if any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate
