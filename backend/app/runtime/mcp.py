"""MCP 服务器：统一编目、智能体分配与运行时物化。

各内核已有的 MCP 配置由适配器的 :meth:`AgentDefinition.discover_mcp_servers` 只读扫描，
与用户在「MCP」页新建的**平台通用**服务器一起汇成统一目录（``mcp_servers`` 表）；
智能体能挂哪些只能通过 ``agent_mcp_servers`` 分配。运行时再由适配器声明「把分配到的
服务器写到哪」（:class:`~app.agents.mcp.McpMount`），框架按声明写进该对话的隔离目录。

密钥（``env`` / ``headers`` 的值）只经 :mod:`app.security` 加密保存：接口永远只回键名与
掩码，明文只存在于「解密后注入适配器」的那一瞬间，不得进日志、事件或对话记录。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, all_agents
from ..agents.mcp import (
    MCP_TRANSPORTS,
    PLATFORM_SOURCE,
    McpMount,
    McpServerSpec,
    safe_slug,
)
from ..models import AgentMcpServer, AgentProfile, McpServer
from ..security import decrypt, encrypt, mask
from .isolation import ensure_home, home_for, note_fresh_home, safe_relative

logger = logging.getLogger("magenta")

#: MCP 来源的中文标签
SOURCE_LABELS = {
    PLATFORM_SOURCE: "平台新建",
    "user": "内核配置",
}

#: 环境变量名与请求头名要能直接写进内核配置，否则内核读不到。
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEADER_KEY = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")


class McpError(ValueError):
    """MCP 操作失败：消息面向用户、可直接展示；``status_code`` 交给 API 层当状态码。"""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- 密钥 -------------------------------------------------------------------


def _encrypt_pairs(pairs: Mapping[str, str] | None) -> dict[str, str]:
    return {str(key): encrypt(str(value)) or "" for key, value in (pairs or {}).items()}


def _decrypt_pairs(raw: Any) -> dict[str, str]:
    """解开「键 → 密文」；解不开或空值的键直接跳过（绝不抛错中断一次运行）。"""
    out: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        plain = decrypt(str(value) if value else None)
        if plain:
            out[str(key)] = plain
    return out


def mask_pairs(raw: Any) -> dict[str, str | None]:
    """给界面的只读视图：键名照旧，值只给掩码。"""
    return {str(key): mask(decrypt(str(value) if value else None)) for key, value in (raw or {}).items()}


def _clean_pairs(raw: Any, *, kind: str) -> dict[str, str]:
    """校验并规整 ``env`` / ``headers`` 的键（值原样保留）。"""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, Mapping):
        raise McpError(f"{'环境变量' if kind == 'env' else '请求头'}必须是「名字 → 值」的键值对")
    pattern = _ENV_KEY if kind == "env" else _HEADER_KEY
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not pattern.match(name):
            hint = (
                "环境变量名只能是字母、数字与下划线，且以字母或下划线开头（例如 API_KEY）"
                if kind == "env"
                else "请求头名只能由字母、数字与 !#$%&'*+.^_`|~- 组成（例如 Authorization）"
            )
            raise McpError(f"{'环境变量' if kind == 'env' else '请求头'}名「{key}」不合法：{hint}")
        cleaned[name] = str(value)
    return cleaned


def _merge_pairs(
    stored: Any, incoming: Mapping[str, str], *, kind: str
) -> dict[str, str]:
    """把界面提交的键值对合并进已存的那份：留空的已有键保持原值，没列出的键删除。"""
    current = _decrypt_pairs(stored)
    cleaned = _clean_pairs(incoming, kind=kind)
    merged: dict[str, str] = {}
    for key, value in cleaned.items():
        text = str(value)
        if text:
            merged[key] = text
        elif key in current:
            merged[key] = current[key]
    return merged


# --- 校验 -------------------------------------------------------------------


def _normalize_transport(raw: Any) -> str:
    value = str(raw or "stdio").strip().lower()
    if value not in MCP_TRANSPORTS:
        raise McpError(f"传输方式「{raw}」不支持：只能是 stdio、http 或 sse")
    return value


def _normalize_connection(transport: str, command: Any, url: Any) -> tuple[str | None, str | None]:
    command = str(command).strip() if command else None
    url = str(url).strip() if url else None
    if transport == "stdio":
        if not command:
            raise McpError("stdio 传输必须填写启动命令（例如 npx）")
        return command, None
    if not url:
        raise McpError("http / sse 传输必须填写服务器地址")
    if not url.startswith(("http://", "https://")):
        raise McpError("服务器地址必须以 http:// 或 https:// 开头")
    return None, url


async def _slug_conflict(session: AsyncSession, slug: str, *, keep_id: str | None = None) -> bool:
    """平台通用服务器之间不允许标识重名（重名写进内核配置会互相覆盖）。"""
    statement = select(McpServer.id).where(
        McpServer.type_id.is_(None),
        McpServer.slug == slug,
        McpServer.deleted_at.is_(None),
    )
    for (mcp_id,) in (await session.execute(statement)).all():
        if mcp_id != keep_id:
            return True
    return False


# --- 编目 -------------------------------------------------------------------


def discover_catalog() -> dict[str, McpServerSpec]:
    """聚合所有适配器扫描到的 MCP 服务器；单个适配器出错不影响其余。"""
    catalog: dict[str, McpServerSpec] = {}
    for definition in all_agents():
        try:
            specs = definition.discover_mcp_servers() or []
        except Exception as exc:  # noqa: BLE001 - 扫描失败不能拖垮整个目录
            logger.warning("扫描智能体 %s 的 MCP 配置失败：%s", definition.id, exc)
            continue
        for spec in specs:
            mcp_id = spec.source_id
            if mcp_id and mcp_id not in catalog:
                catalog[mcp_id] = spec
    return catalog


async def rescan(session: AsyncSession) -> dict[str, int]:
    """重新扫描各内核的 MCP 配置，并同步到统一目录。

    配置里不再存在的服务器只标记 ``available=False``，不删除记录，避免智能体分配断链。
    平台新建的服务器（``type_id`` 为空）不参与重扫。
    """
    discovered = discover_catalog()
    rows = {
        row.id: row
        for row in (
            await session.execute(select(McpServer).where(McpServer.type_id.is_not(None)))
        )
        .scalars()
        .all()
    }
    now = datetime.now(timezone.utc)
    added = updated = 0

    for mcp_id, spec in discovered.items():
        row = rows.get(mcp_id)
        if row is None:
            session.add(
                McpServer(
                    id=mcp_id,
                    name=spec.name,
                    slug=spec.slug,
                    description=spec.description or "",
                    type_id=spec.type_id,
                    source_kind=spec.source_kind,
                    transport=spec.transport,
                    command=spec.command,
                    args=list(spec.args),
                    url=spec.url,
                    env_encrypted=_encrypt_pairs(spec.env),
                    headers_encrypted=_encrypt_pairs(spec.headers),
                    available=True,
                    discovered_at=now,
                    updated_at=now,
                )
            )
            added += 1
            continue
        changed = (
            row.name != spec.name
            or (row.description or "") != (spec.description or "")
            or row.transport != spec.transport
            or (row.command or "") != (spec.command or "")
            or list(row.args or []) != list(spec.args)
            or (row.url or "") != (spec.url or "")
            or _decrypt_pairs(row.env_encrypted) != dict(spec.env)
            or _decrypt_pairs(row.headers_encrypted) != dict(spec.headers)
        )
        if changed or not row.available:
            row.name = spec.name
            row.slug = spec.slug
            row.description = spec.description or ""
            row.source_kind = spec.source_kind
            row.transport = spec.transport
            row.command = spec.command
            row.args = list(spec.args)
            row.url = spec.url
            row.env_encrypted = _encrypt_pairs(spec.env)
            row.headers_encrypted = _encrypt_pairs(spec.headers)
            row.available = True
            row.updated_at = now
            updated += 1

    missing = 0
    for mcp_id, row in rows.items():
        if mcp_id not in discovered and row.available:
            row.available = False
            row.updated_at = now
            missing += 1

    await session.commit()
    return {"added": added, "updated": updated, "missing": missing, "total": len(discovered)}


async def create_row(
    session: AsyncSession,
    *,
    name: str,
    slug: str | None = None,
    description: str | None = None,
    transport: str = "stdio",
    command: str | None = None,
    args: Sequence[Any] | None = None,
    url: str | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    enabled: bool = True,
) -> McpServer:
    """新建一个「平台通用」的 MCP 服务器（可分配给任意内核的智能体）。"""
    label = (name or "").strip()
    if not label:
        raise McpError("请给这个 MCP 服务器起个名字")
    key = safe_slug(slug or label)
    if key is None:
        raise McpError("服务器标识不合法：请使用字母、数字、下划线、点或连字符")
    if await _slug_conflict(session, key):
        raise McpError(f"已经有一个叫「{key}」的 MCP 服务器了，请换一个名字", status_code=409)
    normalized_transport = _normalize_transport(transport)
    normalized_command, normalized_url = _normalize_connection(normalized_transport, command, url)
    now = datetime.now(timezone.utc)
    row = McpServer(
        id=f"mcp-{os.urandom(8).hex()}",
        name=label,
        slug=key,
        description=(description or "").strip(),
        type_id=None,
        source_kind=PLATFORM_SOURCE,
        transport=normalized_transport,
        command=normalized_command,
        args=[str(item) for item in (args or [])],
        url=normalized_url,
        env_encrypted=_encrypt_pairs(_clean_pairs(env, kind="env")),
        headers_encrypted=_encrypt_pairs(_clean_pairs(headers, kind="headers")),
        enabled=bool(enabled),
        available=True,
        discovered_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_row(
    session: AsyncSession,
    row: McpServer,
    *,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    transport: str | None = None,
    command: str | None = None,
    args: Sequence[Any] | None = None,
    url: str | None = None,
    env: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    enabled: bool | None = None,
) -> McpServer:
    """更新一个平台通用的 MCP 服务器（扫描来的服务器不可改，调用方先拦）。"""
    _ensure_editable(row)
    label = (name or row.name).strip()
    if not label:
        raise McpError("请给这个 MCP 服务器起个名字")
    key = safe_slug(slug or label)
    if key is None:
        raise McpError("服务器标识不合法：请使用字母、数字、下划线、点或连字符")
    if await _slug_conflict(session, key, keep_id=row.id):
        raise McpError(f"已经有一个叫「{key}」的 MCP 服务器了，请换一个名字", status_code=409)
    normalized_transport = _normalize_transport(transport if transport is not None else row.transport)
    normalized_command, normalized_url = _normalize_connection(
        normalized_transport,
        command if command is not None else row.command,
        url if url is not None else row.url,
    )
    row.name = label
    row.slug = key
    if description is not None:
        row.description = description.strip()
    row.transport = normalized_transport
    row.command = normalized_command
    row.url = normalized_url
    if args is not None:
        row.args = [str(item) for item in args]
    # 界面回显的是掩码，不能把掩码当新值写回去，所以约定：
    # 列出来的键保留、值留空＝保持原值不变、没列出来的键删除、新键必须给值。
    if env is not None:
        row.env_encrypted = _encrypt_pairs(_merge_pairs(row.env_encrypted, env, kind="env"))
    if headers is not None:
        row.headers_encrypted = _encrypt_pairs(
            _merge_pairs(row.headers_encrypted, headers, kind="headers")
        )
    if enabled is not None:
        row.enabled = bool(enabled)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_row(session: AsyncSession, row: McpServer) -> None:
    """软删除一个平台通用的 MCP 服务器（历史对话与协作历史照旧可读）。"""
    _ensure_editable(row)
    row.deleted_at = datetime.now(timezone.utc)
    row.updated_at = row.deleted_at
    session.add(row)
    await session.commit()


def _ensure_editable(row: McpServer) -> None:
    if row.source_kind != PLATFORM_SOURCE or row.type_id is not None:
        raise McpError(
            f"「{row.name}」是从内核配置扫描出来的，平台不接管它的内容；"
            "请到该内核自己的配置里修改，或新建一个平台通用的服务器",
            status_code=403,
        )


# --- 查询 -------------------------------------------------------------------


async def list_rows(
    session: AsyncSession,
    *,
    type_id: str | None = None,
    source_kind: str | None = None,
    available: bool | None = None,
    query: str | None = None,
) -> list[McpServer]:
    statement = (
        select(McpServer)
        .where(McpServer.deleted_at.is_(None))
        .order_by(McpServer.type_id, McpServer.name)
    )
    if type_id:
        statement = statement.where(McpServer.type_id == type_id)
    if source_kind:
        statement = statement.where(McpServer.source_kind == source_kind)
    if available is not None:
        statement = statement.where(McpServer.available == available)  # noqa: E712
    rows = list((await session.execute(statement)).scalars().all())
    if query:
        needle = query.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.name.lower()
            or needle in row.slug.lower()
            or needle in (row.description or "").lower()
        ]
    return rows


async def get_row(session: AsyncSession, mcp_id: str) -> McpServer | None:
    row = await session.get(McpServer, mcp_id)
    if row is None or row.deleted_at is not None:
        return None
    return row


async def usage_counts(session: AsyncSession) -> dict[str, int]:
    """每个 MCP 服务器被多少个**未删除**的智能体使用。"""
    rows = (
        await session.execute(
            select(AgentMcpServer.mcp_server_id)
            .join(AgentProfile, AgentProfile.id == AgentMcpServer.agent_profile_id)
            .where(AgentProfile.deleted_at.is_(None))
        )
    ).scalars().all()
    counts: dict[str, int] = {}
    for mcp_id in rows:
        counts[mcp_id] = counts.get(mcp_id, 0) + 1
    return counts


async def agents_using(session: AsyncSession, mcp_id: str) -> list[AgentProfile]:
    statement = (
        select(AgentProfile)
        .join(AgentMcpServer, AgentMcpServer.agent_profile_id == AgentProfile.id)
        .where(AgentMcpServer.mcp_server_id == mcp_id, AgentProfile.deleted_at.is_(None))
        .order_by(AgentProfile.created_at)
    )
    return list((await session.execute(statement)).scalars().all())


# --- 分配 -------------------------------------------------------------------


async def agent_mcp_ids(session: AsyncSession, profile_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(AgentMcpServer)
            .where(AgentMcpServer.agent_profile_id == profile_id)
            .order_by(AgentMcpServer.position)
        )
    ).scalars().all()
    return [row.mcp_server_id for row in rows]


async def set_agent_mcp_servers(
    session: AsyncSession, profile: AgentProfile, mcp_ids: list[str]
) -> list[str]:
    """整表替换智能体的 MCP 分配；返回实际写入的 id 列表。

    只接受「可分配给这个内核」的服务器：平台通用的永远可以，扫描来的必须同内核。
    """
    wanted = list(dict.fromkeys(mcp_ids))
    slugs: dict[str, str] = {}
    for mcp_id in wanted:
        row = await get_row(session, mcp_id)
        if row is None:
            raise ValueError(f"MCP 服务器不存在：{mcp_id}")
        if row.type_id is not None and row.type_id != profile.type_id:
            raise ValueError(
                f"MCP 服务器「{row.name}」来自 {row.type_id} 内核的配置，"
                f"与该智能体的内核（{profile.type_id}）不匹配"
            )
        if row.slug in slugs:
            raise ValueError(
                f"分配的服务器里有重名的标识「{row.slug}」"
                f"（{slugs[row.slug]} 与 {row.name}），请只保留一个"
            )
        slugs[row.slug] = row.name

    for link in (
        await session.execute(
            select(AgentMcpServer).where(AgentMcpServer.agent_profile_id == profile.id)
        )
    ).scalars().all():
        await session.delete(link)
    await session.flush()

    for position, mcp_id in enumerate(wanted):
        session.add(
            AgentMcpServer(agent_profile_id=profile.id, mcp_server_id=mcp_id, position=position)
        )
    await session.commit()
    return wanted


async def mcp_servers_for_agent(
    session: AsyncSession, profile: AgentProfile
) -> list[McpServerSpec]:
    """智能体在本轮运行中真正生效的 MCP 服务器（只取可分配、可用且启用中的）。"""
    ids = await agent_mcp_ids(session, profile.id)
    if not ids:
        return []
    rows = (await session.execute(select(McpServer).where(McpServer.id.in_(ids)))).scalars().all()
    by_id = {row.id: row for row in rows}
    specs: list[McpServerSpec] = []
    for mcp_id in ids:
        row = by_id.get(mcp_id)
        if row is None or row.deleted_at is not None or not row.available or not row.enabled:
            continue
        if row.type_id is not None and row.type_id != profile.type_id:
            continue
        specs.append(spec_from_row(row))
    return specs


def spec_from_row(row: McpServer) -> McpServerSpec:
    """把库里的行翻译成适配器要的 :class:`McpServerSpec`（这里是唯一解密的地方）。"""
    return McpServerSpec(
        name=row.name,
        slug=row.slug,
        description=row.description or "",
        transport=row.transport,
        command=row.command,
        args=list(row.args or []),
        url=row.url,
        env=_decrypt_pairs(row.env_encrypted),
        headers=_decrypt_pairs(row.headers_encrypted),
        type_id=row.type_id,
        source_kind=row.source_kind,
    )


def read_view(row: McpServer, *, used_by: int = 0, type_name: str | None = None) -> dict[str, Any]:
    """给界面的只读视图：密钥只给键名与掩码，永远没有明文。"""
    return {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description or "",
        "type_id": row.type_id,
        "type_name": type_name,
        "source_kind": row.source_kind,
        "source_label": SOURCE_LABELS.get(row.source_kind, row.source_kind),
        "transport": row.transport,
        "command": row.command,
        "args": list(row.args or []),
        "url": row.url,
        "env_keys": sorted(_decrypt_pairs(row.env_encrypted).keys()),
        "env_preview": mask_pairs(row.env_encrypted),
        "header_keys": sorted(_decrypt_pairs(row.headers_encrypted).keys()),
        "header_preview": mask_pairs(row.headers_encrypted),
        "enabled": bool(row.enabled),
        "available": bool(row.available),
        "note": row.note,
        "editable": row.source_kind == PLATFORM_SOURCE and row.type_id is None,
        "used_by_agents": used_by,
    }


# --- 物化 -------------------------------------------------------------------


async def prepare_mcp_home(
    session: AsyncSession,
    definition: AgentDefinition,
    profile: AgentProfile,
    conversation: Any,
    ctx: AgentContext,
    *,
    scope_id: str | None = None,
) -> list[str]:
    """把分配到的 MCP 服务器写进该内核的隔离目录，并把结果写回 ``ctx``。

    ``scope_id`` 是隔离目录的作用域（默认用对话 id；协作运行会传运行 id）。
    返回需要提示给用户的说明/告警（由运行层作为 ``log`` 事件暴露）。
    """
    ctx.mcp_servers = await mcp_servers_for_agent(session, profile)
    mount = definition.mcp_mount(ctx)
    if mount is None:
        return []
    warnings: list[str] = []
    # 只有当配置真的要写进「内核隔离配置目录」时才建这个家：技能与 MCP 共用同一个目录，
    # 技能物化（prepare_skill_home）已经定过目录时以它为准。只写工作目录的内核
    # （例如 Gemini CLI）不需要这个目录，凭空建一个空目录会被 note_fresh_home
    # 误判成「新家」而清掉会话。
    if mount.files or ctx.home_dir:
        home = (
            Path(ctx.home_dir)
            if ctx.home_dir
            else home_for(scope_id or conversation.id, mount.home_name)
        )
        first_run = not home.exists()
        warnings += ensure_home(home)
        warnings += _write_files(home, mount.files, label="内核配置")
        ctx.home_dir = str(home)
        warnings += note_fresh_home(ctx, first_run=first_run)
    if mount.workspace_files:
        if ctx.workspace_dir:
            warnings += _write_files(
                Path(ctx.workspace_dir), mount.workspace_files, label="项目配置"
            )
        else:
            warnings.append("这个内核的 MCP 配置要写进工作目录，但本轮没有工作目录，已跳过")
    return [*mount.notes, *warnings]


def _write_files(base: Path, files: Mapping[str, str], *, label: str) -> list[str]:
    """把「相对路径 → 内容」写进某个目录（内容可能含密钥，一律收紧权限）。"""
    warnings: list[str] = []
    for relative, content in (files or {}).items():
        target_rel = safe_relative(relative)
        if target_rel is None:
            warnings.append(f"{label}的写入路径不安全，已跳过：{relative}")
            continue
        target = base / target_rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            os.chmod(target, 0o600)
        except OSError as exc:
            warnings.append(f"写入{label} {relative} 失败：{exc}")
    return warnings


__all__ = ["McpError", "McpMount", "McpServerSpec", "SOURCE_LABELS"]
