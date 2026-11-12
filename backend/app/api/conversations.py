from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import attachments
from ..db import SessionLocal, get_session
from ..models import AgentProfile, Conversation, Message
from ..runtime import registry
from ..runtime import skills as skill_service
from ..runtime.service import (
    ConfigurationError,
    consume_run,
    new_conversation,
    pick_default_profile,
    prepare_run,
    regenerate_last,
    resolve_workspace,
)
from ..schemas import (
    AttachmentUpload,
    ConversationCreate,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
    SendMessage,
)
from ..transport.sse import sse_stream

router = APIRouter(prefix="/conversations", tags=["conversations"])

#: 单个附件的上限：手机上多为截图与文档，8 MB 足够，也免得太大的 base64 撑爆请求体。
MAX_ATTACHMENT_BYTES = attachments.MAX_ATTACHMENT_BYTES


async def _related(session: AsyncSession, conversation: Conversation) -> AgentProfile | None:
    """取对话绑定的智能体（创建时的快照）。

    只给「单条响应」用；列表接口走批量查询，避免查询数随对话数线性增长（N+1）。
    """
    return await session.get(AgentProfile, conversation.agent_profile_id)


def _conversation_read(
    conversation: Conversation,
    profile: AgentProfile | None,
    count: int = 0,
) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        agent_profile_id=conversation.agent_profile_id,
        session_id=conversation.session_id,
        workspace_dir=conversation.workspace_dir,
        archived=conversation.archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        agent_name=profile.name if profile else None,
        agent_avatar=profile.avatar if profile else None,
        agent_type_id=profile.type_id if profile else None,
        message_count=count,
    )


@router.get("")
async def list_conversations(
    include_archived: bool = False, session: AsyncSession = Depends(get_session)
) -> dict:
    query = select(Conversation).order_by(Conversation.updated_at.desc())
    if not include_archived:
        query = query.where(Conversation.archived == False)  # noqa: E712
    rows = (await session.execute(query)).scalars().all()

    counts = dict(
        (await session.execute(select(Message.conversation_id, func.count(Message.id)).group_by(Message.conversation_id))).all()
    )
    # 智能体批量取一次：逐个 session.get 会让一次列表请求打出 N 条查询。
    profile_ids = {c.agent_profile_id for c in rows}
    profiles = {
        row.id: row
        for row in (
            await session.execute(select(AgentProfile).where(AgentProfile.id.in_(profile_ids)))
        ).scalars()
    }
    items = [
        _conversation_read(c, profiles.get(c.agent_profile_id), counts.get(c.id, 0)).model_dump()
        for c in rows
    ]
    return {"items": items, "active_runs": registry.list()}


@router.post("", status_code=201)
async def create_conversation(
    payload: ConversationCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    """新建对话：不指定智能体就用「默认智能体」（卡片右上角那颗五角星）。

    工作目录同理可以留空（按对话自动分配独立沙箱），进去之后在聊天页输入框上方
    那两个小图标里随时改 —— 所以「新建」这一步不再需要先做任何选择。
    """
    if payload.agent_profile_id:
        profile = await session.get(AgentProfile, payload.agent_profile_id)
        if profile is None or profile.deleted_at is not None:
            raise HTTPException(status_code=422, detail="指定的智能体不存在")
    else:
        profile = await pick_default_profile(session)
        if profile is None:
            raise HTTPException(
                status_code=422,
                detail="还没有可用的智能体：先到「智能体」页创建，并点卡片右上角的五角星设为默认",
            )
    conversation = new_conversation(profile, payload.title, payload.workspace_dir)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return _conversation_read(conversation, profile, 0).model_dump()


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    messages = (
        await session.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
        )
    ).scalars().all()
    profile = await _related(session, conversation)
    return {
        "conversation": _conversation_read(conversation, profile, len(messages)).model_dump(),
        "messages": [MessageRead.model_validate(m).model_dump() for m in messages],
        "active_run": registry.active_for(conversation_id).snapshot() if registry.active_for(conversation_id) else None,
    }


@router.patch("/{conversation_id}")
async def update_conversation(
    conversation_id: str, payload: ConversationUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    data = payload.model_dump(exclude_unset=True)

    # 换智能体 / 换工作目录都要让会话记忆重来：内核的 session 是绑在「智能体 + 工作目录」
    # 上的，留着上一个内核的会话 id 只会把新的一轮带偏（CLI 甚至直接报 resume 失败）。
    scope_changed = False
    if data.get("agent_profile_id") and data["agent_profile_id"] != conversation.agent_profile_id:
        profile = await session.get(AgentProfile, data.pop("agent_profile_id"))
        if profile is None or profile.deleted_at is not None:
            raise HTTPException(status_code=422, detail="指定的智能体不存在")
        conversation.agent_profile_id = profile.id
        scope_changed = True
    if "workspace_dir" in data:
        workspace = (data.pop("workspace_dir") or "").strip()
        if workspace != (conversation.workspace_dir or ""):
            scope_changed = True
        conversation.workspace_dir = workspace or None
    if scope_changed and registry.active_for(conversation_id):
        raise HTTPException(status_code=409, detail="这条对话正在运行，先停止或等它结束再改")
    if scope_changed:
        conversation.session_id = None

    for field, value in data.items():
        if value is not None:
            setattr(conversation, field, value)
    conversation.updated_at = datetime.now(timezone.utc)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    profile = await _related(session, conversation)
    return _conversation_read(conversation, profile).model_dump()


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    registry.cancel_for_conversation(conversation_id)
    # 用单条 DELETE 显式清空消息（保留「删对话必须清消息」的不变量），比逐行
    # session.delete 少一层往返，也避开 SQLite 驱动对 executemany 行数统计为 0 的误导性告警。
    await session.execute(delete(Message).where(Message.conversation_id == conversation_id))
    await session.delete(conversation)
    await session.commit()
    # 该对话的内核隔离配置目录（含分配技能的副本）一并清掉。
    skill_service.cleanup_conversation(conversation_id)
    return Response(status_code=204)


@router.post("/{conversation_id}/session/reset")
async def reset_session(conversation_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    conversation.session_id = None
    session.add(conversation)
    await session.commit()
    return {"ok": True, "session_id": None}


@router.post("/{conversation_id}/attachments", status_code=201)
async def upload_attachment(
    conversation_id: str, payload: AttachmentUpload, session: AsyncSession = Depends(get_session)
) -> dict:
    """把附件存进对话的工作目录（`<工作目录>/attachments/`）。

    附件不经过适配器：智能体本来就能读工作目录里的文件，发消息时把相对路径写进提示词即可
    （见 `runtime/service.compose_user_content`），因此每个内核都自动支持。
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    profile = await session.get(AgentProfile, conversation.agent_profile_id)
    if profile is None:
        raise HTTPException(status_code=409, detail="对话绑定的智能体已不存在，无法保存附件")

    try:
        raw = attachments.decode_attachment(payload.content_base64, limit=MAX_ATTACHMENT_BYTES)
        workspace = Path(resolve_workspace(conversation))
        return attachments.store_attachment(workspace, payload.name, raw).to_dict()
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _stream_prepared(prepared) -> StreamingResponse:
    async def event_source() -> AsyncIterator[dict]:
        async with SessionLocal() as session:
            conversation = await session.get(Conversation, prepared.conversation.id)
            assistant = await session.get(Message, prepared.assistant_message.id)
            if conversation is not None:
                prepared.conversation = conversation
            if assistant is not None:
                prepared.assistant_message = assistant
            yield {
                "type": "turn.started",
                "run_id": prepared.run.id,
                "conversation_id": prepared.conversation.id,
                "user_message_id": prepared.user_message.id,
                "assistant_message_id": prepared.assistant_message.id,
                "agent": prepared.profile.name,
            }
            async for event in consume_run(session, prepared):
                yield event.to_dict()

    async def guarded() -> AsyncIterator[str]:
        try:
            async for chunk in sse_stream(event_source()):
                yield chunk
        except asyncio.CancelledError:
            registry.cancel(prepared.run.id)
            raise

    return StreamingResponse(
        guarded(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str, payload: SendMessage, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    try:
        prepared = await prepare_run(session, conversation_id, payload)
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_prepared(prepared)


@router.post("/{conversation_id}/regenerate")
async def regenerate(conversation_id: str, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    try:
        prepared = await regenerate_last(session, conversation_id)
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_prepared(prepared)


@router.post("/{conversation_id}/cancel")
async def cancel_run(conversation_id: str) -> dict:
    cancelled = registry.cancel_for_conversation(conversation_id)
    return {"ok": cancelled}
