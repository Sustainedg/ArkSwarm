"""MCP 服务器：统一编目与智能体分配。

各内核已有的 MCP 配置由适配器只读扫描，与用户新建的「平台通用」服务器一起汇成一份
统一目录；智能体能挂哪些只能通过这里的分配关系获得（``agent_mcp_servers`` 表）。
密钥永远只回键名与掩码。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import get_agent
from ..db import get_session
from ..models import McpServer, User
from ..runtime import mcp as mcp_service
from ..schemas import (
    McpRescanResult,
    McpServerCreate,
    McpServerDetail,
    McpServerRead,
    McpServerUpdate,
)
from .auth import current_user, owner_id_for, require_owned

router = APIRouter(prefix="/mcp-servers", tags=["mcp"])


def _type_name(type_id: str | None) -> str | None:
    if not type_id:
        return None
    try:
        return get_agent(type_id).name
    except KeyError:
        return None


def _describe(row: McpServer, counts: dict[str, int]) -> McpServerRead:
    return McpServerRead(
        **mcp_service.read_view(
            row, used_by=counts.get(row.id, 0), type_name=_type_name(row.type_id)
        )
    )


async def _active_row(mcp_id: str, session: AsyncSession) -> McpServer:
    row = await mcp_service.get_row(session, mcp_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"MCP 服务器不存在：{mcp_id}")
    return row


def _require_visible(row: McpServer, user: User) -> None:
    """列表里能看到的行，详情 / 编辑入口也要能进。

    扫描来的服务器（``type_id`` 非空）是各内核配置的共享目录，所有登录用户可读；
    平台通用服务器按 ``owner_id`` 隔离。内置服务器按用户各播种一份，管理员也只能
    打开自己的那一份（系统管理 MCP 的 PAT 不跨用户共用）。写操作仍由
    ``_ensure_editable`` 拒绝扫描项。
    """
    if row.builtin_key is not None:
        if row.owner_id != owner_id_for(user):
            raise HTTPException(status_code=404, detail="资源不存在或无权访问")
        return
    if row.type_id is None:
        require_owned(row, user)


@router.get("")
async def list_mcp_servers(
    type_id: str | None = None,
    source_kind: str | None = None,
    available: bool | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """列出统一目录里的 MCP 服务器（可按来源内核 / 来源 / 可用性 / 关键词过滤）。

    非管理员只看到自己的平台通用服务器 + 各内核配置扫来的共享服务器；管理员看全部
    自定义服务器与扫描项，但内置服务器同样只看自己那一份（不跨用户展示重复副本）。
    """
    owner_id = None if user.role == "admin" else user.id
    rows = await mcp_service.list_rows(
        session,
        type_id=type_id,
        source_kind=source_kind,
        available=available,
        query=q,
        owner_id=owner_id,
        builtin_owner_id=owner_id_for(user),
        scope_builtins=True,
    )
    counts = await mcp_service.usage_counts(session, owner_id)
    return {
        "items": [_describe(row, counts).model_dump() for row in rows],
        "source_labels": mcp_service.SOURCE_LABELS,
    }


@router.post("", status_code=201)
async def create_mcp_server(
    payload: McpServerCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """新建一个「平台通用」的 MCP 服务器（可分配给任意内核的智能体）。"""
    try:
        row = await mcp_service.create_row(
            session,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            transport=payload.transport,
            command=payload.command,
            args=payload.args,
            url=payload.url,
            env=payload.env,
            headers=payload.headers,
            enabled=payload.enabled,
            owner_id=owner_id_for(user),
        )
    except mcp_service.McpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return _describe(row, {}).model_dump()


@router.post("/rescan")
async def rescan_mcp_servers(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """重新扫描各内核配置里的 MCP 服务器，并同步到统一目录。"""
    result = await mcp_service.rescan(session, owner_id=owner_id_for(user))
    return McpRescanResult(**result).model_dump()


@router.get("/{mcp_id}")
async def get_mcp_server(
    mcp_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    row = await _active_row(mcp_id, session)
    _require_visible(row, user)
    counts = await mcp_service.usage_counts(
        session, None if user.role == "admin" else user.id
    )
    agents = await mcp_service.agents_using(session, mcp_id)
    if user.role != "admin":
        # 共享的扫描项对所有登录用户可见，但「谁在用」只该列出自己的智能体。
        agents = [profile for profile in agents if profile.owner_id == user.id]
    detail = McpServerDetail(
        **_describe(row, counts).model_dump(),
        agents=[
            {"id": profile.id, "name": profile.name, "avatar": profile.avatar}
            for profile in agents
        ],
    )
    return detail.model_dump()


@router.patch("/{mcp_id}")
async def update_mcp_server(
    mcp_id: str,
    payload: McpServerUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """更新一个平台通用的 MCP 服务器（扫描来的由内核配置说了算，改不了）。"""
    row = await _active_row(mcp_id, session)
    _require_visible(row, user)
    try:
        row = await mcp_service.update_row(
            session,
            row,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            transport=payload.transport,
            command=payload.command,
            args=payload.args,
            url=payload.url,
            env=payload.env,
            headers=payload.headers,
            enabled=payload.enabled,
            owner_id=row.owner_id,
        )
    except mcp_service.McpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    counts = await mcp_service.usage_counts(
        session, None if user.role == "admin" else user.id
    )
    return _describe(row, counts).model_dump()


@router.delete("/{mcp_id}", status_code=204)
async def delete_mcp_server(
    mcp_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """删除一个平台通用的 MCP 服务器（软删除）。

    仍被**未删除**的智能体引用时返回 409：先解除分配，避免那些智能体运行时悄悄少了一组工具。
    """
    row = await _active_row(mcp_id, session)
    _require_visible(row, user)
    used = await mcp_service.agents_using(session, row.id)
    if user.role != "admin":
        used = [profile for profile in used if profile.owner_id == user.id]
    if used:
        names = "、".join(profile.name for profile in used[:5])
        raise HTTPException(
            status_code=409,
            detail=f"还有 {len(used)} 个智能体挂着这个 MCP 服务器（{names}），请先解除分配再删除",
        )
    try:
        await mcp_service.delete_row(session, row)
    except mcp_service.McpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return Response(status_code=204)
