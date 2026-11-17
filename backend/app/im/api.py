"""IM 的 HTTP 接口：连接增删改、配对、聊天绑定。

这一层只做参数校验、归属过滤与状态码；连接的启停与消息路由都在
:mod:`app.im.service`。凭据只经 :mod:`app.security` 加密落库，接口只回「哪些字段已填」。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from ..auth_deps import current_user, owner_id_for, require_owned
from ..db import get_session
from ..models import AgentProfile, User
from ..security import encrypt
from .base import (
    AdapterError,
    clean_fields,
    clean_options,
    platform_dicts,
    platform_of,
    required_fields,
    scrub,
)
from .platforms import adapter_for
from .schemas import (
    ImChatRead,
    ImChatUpdate,
    ImConnectionCreate,
    ImConnectionRead,
    ImConnectionUpdate,
    ImPairRequest,
    ImPeerRead,
)
from .service import ImError, manager, runtime_fields
from .tables import ImChat, ImConnection, ImPeer

router = APIRouter(prefix="/im", tags=["im"])


# --- 视图 -------------------------------------------------------------------


def _profile_name(profiles: dict[str, AgentProfile], profile_id: str | None) -> str | None:
    profile = profiles.get(profile_id) if profile_id else None
    return profile.name if profile is not None else None


def _state_of(row: ImConnection) -> tuple[str, str | None]:
    """连接的真实状态：活着的以运行时为准，否则看库里最后记下的。"""
    live = manager().status_of(row.id)
    if live is not None:
        return str(live.get("state") or "connecting"), live.get("error")
    return row.last_status or "off", row.last_error


def _to_read(
    row: ImConnection,
    profile: AgentProfile | None,
    counts: dict[str, int],
) -> ImConnectionRead:
    platform = platform_of(row.platform)
    state, error = _state_of(row)
    return ImConnectionRead(
        id=row.id,
        platform=row.platform,
        platform_name=platform.name if platform is not None else row.platform,
        account_name=row.account_name,
        account_id=row.account_id,
        agent_profile_id=row.agent_profile_id,
        agent_name=profile.name if profile is not None else None,
        agent_avatar=profile.avatar if profile is not None else None,
        enabled=row.enabled,
        state=state,
        error=error,
        secret_fields=[key for key, value in (row.secrets or {}).items() if value],
        options=clean_options(row.options),
        created_at=row.created_at,
        updated_at=row.updated_at,
        peers=counts.get("peers", 0),
        pending=counts.get("pending", 0),
        chats=counts.get("chats", 0),
    )


async def _counts(session: AsyncSession, connection_id: str) -> dict[str, int]:
    peers = (
        await session.execute(
            select(func.count(ImPeer.id)).where(
                ImPeer.connection_id == connection_id, ImPeer.state == "allowed"
            )
        )
    ).scalar_one()
    pending = (
        await session.execute(
            select(func.count(ImPeer.id)).where(
                ImPeer.connection_id == connection_id, ImPeer.state == "pending"
            )
        )
    ).scalar_one()
    chats = (
        await session.execute(
            select(func.count(ImChat.id)).where(ImChat.connection_id == connection_id)
        )
    ).scalar_one()
    return {"peers": int(peers), "pending": int(pending), "chats": int(chats)}


async def _owned_connection(connection_id: str, session: AsyncSession, user: User) -> ImConnection:
    row = await session.get(ImConnection, connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="IM 机器人不存在")
    require_owned(row, user)
    return row


def _owned_query(stmt, user: User):
    if user.role == "admin":
        return stmt
    return stmt.where(ImConnection.owner_id == user.id)


async def _check_profile(
    session: AsyncSession, agent_profile_id: str | None, user: User
) -> AgentProfile | None:
    if not agent_profile_id:
        return None
    profile = await session.get(AgentProfile, agent_profile_id)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=422, detail="指定的智能体不存在")
    require_owned(profile, user)
    return profile


def _apply_fields(row: ImConnection, fields: dict) -> None:
    """按平台声明写入非密钥字段与密钥（密钥留空＝保留旧值）。"""
    platform = platform_of(row.platform)
    if platform is None:
        raise HTTPException(status_code=422, detail="不支持的平台")
    cleaned = clean_fields(platform, fields, dict(row.fields or {}))
    stored = dict(row.fields or {})
    secrets = dict(row.secrets or {})
    for item in platform.fields:
        if item.secret:
            value = fields.get(item.key)
            if isinstance(value, str) and value.strip():
                secrets[item.key] = encrypt(value.strip()) or ""
            continue
        if item.key in cleaned:
            stored[item.key] = cleaned[item.key]
    row.fields = stored
    row.secrets = secrets


def _validate_fields(row: ImConnection) -> None:
    platform = platform_of(row.platform)
    if platform is None:
        raise HTTPException(status_code=422, detail="不支持的平台")
    missing = [
        item.label
        for item in platform.fields
        if item.key in required_fields(platform)
        and not str((row.fields or {}).get(item.key) or "").strip()
        and not (row.secrets or {}).get(item.key)
    ]
    if missing:
        raise HTTPException(status_code=422, detail=f"请先填写：{'、'.join(missing)}")
    for item in platform.fields:
        if item.secret and not (row.secrets or {}).get(item.key):
            raise HTTPException(status_code=422, detail=f"请先填写：{item.label}")


# --- 平台元数据 -------------------------------------------------------------


@router.get("/platforms")
async def list_platforms(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """设置页的「可连接平台」清单（添加表单由它生成）。"""
    return {"items": platform_dicts()}


# --- 连接 -------------------------------------------------------------------


@router.get("/connections")
async def list_connections(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    query = _owned_query(select(ImConnection).order_by(ImConnection.created_at), user)
    rows = (await session.execute(query)).scalars().all()
    profile_ids = {row.agent_profile_id for row in rows if row.agent_profile_id}
    profiles = {
        profile.id: profile
        for profile in (
            await session.execute(select(AgentProfile).where(AgentProfile.id.in_(profile_ids)))
        ).scalars()
    }
    items = []
    for row in rows:
        counts = await _counts(session, row.id)
        items.append(_to_read(row, profiles.get(row.agent_profile_id), counts).model_dump())
    return {"items": items}


@router.post("/connections", status_code=201)
async def create_connection(
    payload: ImConnectionCreate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """新建一条连接：先向平台核对一次，核对通过才保存。

    与网关的「必须成功才能保存」是同一条服务端不变量：凭据错在这里就能看到原因，
    而不是存进库之后让连接悄悄连不上。
    """
    platform = platform_of(payload.platform)
    if platform is None:
        raise HTTPException(status_code=422, detail="不支持的平台")
    if not platform.available:
        raise HTTPException(
            status_code=422, detail=platform.note or "这个平台暂不支持连接"
        )
    adapter = adapter_for(platform.id)
    if adapter is None:
        raise HTTPException(status_code=422, detail="这个平台暂不支持连接")
    profile = await _check_profile(session, payload.agent_profile_id, user)
    if profile is None:
        raise HTTPException(status_code=422, detail="请先选择要对话的智能体")

    row = ImConnection(
        platform=platform.id,
        agent_profile_id=profile.id,
        options=clean_options(payload.options.model_dump()),
        enabled=payload.enabled,
        owner_id=owner_id_for(user),
    )
    _apply_fields(row, payload.fields)
    _validate_fields(row)
    try:
        account = await adapter.check(runtime_fields(row))
    except AdapterError as exc:
        raise HTTPException(
            status_code=422, detail=f"平台没有通过核对：{scrub(str(exc))}"
        ) from None
    row.account_id = account.id
    row.account_name = account.name
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await manager().sync(row.id)
    row = await session.get(ImConnection, row.id) or row
    counts = await _counts(session, row.id)
    return _to_read(row, profile, counts).model_dump()


@router.patch("/connections/{connection_id}")
async def update_connection(
    connection_id: str,
    payload: ImConnectionUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    row = await _owned_connection(connection_id, session, user)
    profile: AgentProfile | None = None
    # 显式传 null 表示解绑；没传这个键才是「不改」。
    if "agent_profile_id" in payload.model_fields_set:
        if not payload.agent_profile_id:
            row.agent_profile_id = None
        else:
            profile = await _check_profile(session, payload.agent_profile_id, user)
            row.agent_profile_id = profile.id
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.options is not None:
        # 只覆盖传上来的开关，没传的键保持原值（部分更新）。
        merged = clean_options(row.options)
        for key in payload.options.model_fields_set:
            merged[key] = bool(getattr(payload.options, key))
        row.options = clean_options(merged)
    if payload.fields is not None:
        _apply_fields(row, payload.fields)
        _validate_fields(row)
        adapter = adapter_for(row.platform)
        if adapter is None:
            raise HTTPException(status_code=422, detail="这个平台暂不支持连接")
        try:
            account = await adapter.check(runtime_fields(row))
        except AdapterError as exc:
            raise HTTPException(
                status_code=422, detail=f"平台没有通过核对：{scrub(str(exc))}"
            ) from None
        row.account_id = account.id
        row.account_name = account.name
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await manager().sync(row.id)
    row = await session.get(ImConnection, row.id) or row
    if profile is None and row.agent_profile_id:
        profile = await session.get(AgentProfile, row.agent_profile_id)
    counts = await _counts(session, row.id)
    return _to_read(row, profile, counts).model_dump()


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """移除一条连接：停掉它，连同配对账号与聊天绑定一起删（凭据也随之消失）。"""
    row = await _owned_connection(connection_id, session, user)
    await manager().stop(connection_id)
    for peer in (
        await session.execute(select(ImPeer).where(ImPeer.connection_id == connection_id))
    ).scalars().all():
        await session.delete(peer)
    for chat in (
        await session.execute(select(ImChat).where(ImChat.connection_id == connection_id))
    ).scalars().all():
        await session.delete(chat)
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


@router.post("/connections/{connection_id}/retry")
async def retry_connection(
    connection_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """重新连一次（状态卡在 error 时的「重试」按钮）。"""
    row = await _owned_connection(connection_id, session, user)
    try:
        await manager().sync(row.id)
    except ImError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return {"ok": True, "state": manager().status_of(row.id)}


# --- 配对 -------------------------------------------------------------------


@router.get("/peers")
async def list_peers(
    state: str | None = None,
    connection_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """列出配对请求与已配对账号（设置页的「谁想连接」）。"""
    owned = (await session.execute(_owned_query(select(ImConnection.id), user))).scalars().all()
    if not owned:
        return {"items": []}
    stmt = select(ImPeer).where(ImPeer.connection_id.in_(owned))
    if state:
        stmt = stmt.where(ImPeer.state == state)
    if connection_id:
        if connection_id not in owned:
            raise HTTPException(status_code=404, detail="IM 机器人不存在")
        stmt = stmt.where(ImPeer.connection_id == connection_id)
    rows = (await session.execute(stmt.order_by(ImPeer.created_at))).scalars().all()
    return {"items": [ImPeerRead(**row.model_dump()).model_dump() for row in rows]}


@router.post("/connections/{connection_id}/peers/{peer_id}")
async def answer_peer(
    connection_id: str,
    peer_id: str,
    payload: ImPairRequest,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """允许 / 拒绝一个连接请求；允许时顺手建出那条聊天。"""
    row = await _owned_connection(connection_id, session, user)
    peer = await session.get(ImPeer, peer_id)
    if peer is None or peer.connection_id != row.id:
        raise HTTPException(status_code=404, detail="这条连接请求已经不在了")
    peer.state = "allowed" if payload.allow else "denied"
    peer.updated_at = datetime.now(timezone.utc)
    if payload.allow and peer.chat_key:
        exists = (
            await session.execute(
                select(ImChat).where(
                    ImChat.connection_id == row.id, ImChat.chat_key == peer.chat_key
                )
            )
        ).scalars().first()
        if exists is None:
            session.add(
                ImChat(
                    connection_id=row.id,
                    chat_key=peer.chat_key,
                    kind=peer.chat_kind,
                    title=peer.chat_title or peer.sender_name,
                )
            )
    session.add(peer)
    await session.commit()
    await session.refresh(peer)
    return ImPeerRead(**peer.model_dump()).model_dump()


@router.delete("/connections/{connection_id}/peers/{peer_id}", status_code=204)
async def remove_peer(
    connection_id: str,
    peer_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    """移除一个已配对账号：之后它发的消息不再进智能体。"""
    row = await _owned_connection(connection_id, session, user)
    peer = await session.get(ImPeer, peer_id)
    if peer is None or peer.connection_id != row.id:
        raise HTTPException(status_code=404, detail="这个账号已经不在了")
    await session.delete(peer)
    await session.commit()
    return Response(status_code=204)


# --- 聊天 -------------------------------------------------------------------


@router.get("/connections/{connection_id}/chats")
async def list_chats(
    connection_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    row = await _owned_connection(connection_id, session, user)
    rows = (
        await session.execute(
            select(ImChat).where(ImChat.connection_id == row.id).order_by(ImChat.updated_at.desc())
        )
    ).scalars().all()
    profiles = {
        profile.id: profile
        for profile in (
            await session.execute(
                select(AgentProfile).where(
                    AgentProfile.id.in_(
                        {item.agent_profile_id for item in rows if item.agent_profile_id}
                    )
                )
            )
        ).scalars()
    }
    items = [
        ImChatRead(
            id=item.id,
            connection_id=item.connection_id,
            chat_key=item.chat_key,
            kind=item.kind,
            title=item.title,
            agent_profile_id=item.agent_profile_id,
            agent_name=_profile_name(profiles, item.agent_profile_id),
            conversation_id=item.conversation_id,
            updated_at=item.updated_at,
        ).model_dump()
        for item in rows
    ]
    return {"items": items}


@router.patch("/connections/{connection_id}/chats/{chat_id}")
async def update_chat(
    connection_id: str,
    chat_id: str,
    payload: ImChatUpdate,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """给某个聊天单独指定智能体（群里常见）；传 null 表示跟随连接。"""
    row = await _owned_connection(connection_id, session, user)
    chat = await session.get(ImChat, chat_id)
    if chat is None or chat.connection_id != row.id:
        raise HTTPException(status_code=404, detail="这个聊天不在了")
    profile = None
    if payload.agent_profile_id:
        profile = await _check_profile(session, payload.agent_profile_id, user)
        chat.agent_profile_id = profile.id
    else:
        chat.agent_profile_id = None
    chat.updated_at = datetime.now(timezone.utc)
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    if profile is None and chat.agent_profile_id:
        profile = await session.get(AgentProfile, chat.agent_profile_id)
    return ImChatRead(
        id=chat.id,
        connection_id=chat.connection_id,
        chat_key=chat.chat_key,
        kind=chat.kind,
        title=chat.title,
        agent_profile_id=chat.agent_profile_id,
        agent_name=profile.name if profile is not None else None,
        conversation_id=chat.conversation_id,
        updated_at=chat.updated_at,
    ).model_dump()


@router.delete("/connections/{connection_id}/chats/{chat_id}", status_code=204)
async def remove_chat(
    connection_id: str,
    chat_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> Response:
    row = await _owned_connection(connection_id, session, user)
    chat = await session.get(ImChat, chat_id)
    if chat is None or chat.connection_id != row.id:
        raise HTTPException(status_code=404, detail="这个聊天不在了")
    await session.delete(chat)
    await session.commit()
    return Response(status_code=204)


__all__ = ["router"]
