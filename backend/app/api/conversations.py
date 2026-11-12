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
from ..models import AgentInstance, Conversation, Message, Role
from ..runtime import registry
from ..runtime import skills as skill_service
from ..runtime.service import (
    ConfigurationError,
    consume_run,
    new_conversation,
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


async def _related(
    session: AsyncSession, conversation: Conversation
) -> tuple[Role | None, AgentInstance | None]:
    """取对话引用的角色与智能体快照。

    只给「单条响应」用；列表接口走批量查询，避免查询数随对话数线性增长（N+1）。
    """
    role = await session.get(Role, conversation.role_id)
    agent = await session.get(AgentInstance, conversation.agent_id)
    return role, agent


def _conversation_read(
    conversation: Conversation,
    role: Role | None,
    agent: AgentInstance | None,
    count: int = 0,
) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        role_id=conversation.role_id,
        agent_id=conversation.agent_id,
        session_id=conversation.session_id,
        workspace_dir=conversation.workspace_dir,
        archived=conversation.archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        role_name=role.name if role else None,
        role_avatar=role.avatar if role else None,
        agent_name=agent.name if agent else None,
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
    # 角色与智能体各批量取一次：逐个 session.get 会让一次列表请求打出 2N 条查询。
    role_ids = {c.role_id for c in rows}
    agent_ids = {c.agent_id for c in rows}
    roles = {
        r.id: r for r in (await session.execute(select(Role).where(Role.id.in_(role_ids)))).scalars()
    }
    agents = {
        a.id: a
        for a in (
            await session.execute(select(AgentInstance).where(AgentInstance.id.in_(agent_ids)))
        ).scalars()
    }
    items = [
        _conversation_read(c, roles.get(c.role_id), agents.get(c.agent_id), counts.get(c.id, 0)).model_dump()
        for c in rows
    ]
    return {"items": items, "active_runs": registry.list()}


@router.post("", status_code=201)
async def create_conversation(
    payload: ConversationCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    role = await session.get(Role, payload.role_id)
    if role is None or role.deleted_at is not None:
        raise HTTPException(status_code=422, detail="指定的 role_id 不存在")
    conversation = new_conversation(role, payload.title, payload.workspace_dir)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    agent = await session.get(AgentInstance, conversation.agent_id)
    return _conversation_read(conversation, role, agent, 0).model_dump()


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
    role, agent = await _related(session, conversation)
    return {
        "conversation": _conversation_read(conversation, role, agent, len(messages)).model_dump(),
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
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(conversation, field, value)
    conversation.updated_at = datetime.now(timezone.utc)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    role, agent = await _related(session, conversation)
    return _conversation_read(conversation, role, agent).model_dump()


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
    role = await session.get(Role, conversation.role_id)
    if role is None:
        raise HTTPException(status_code=409, detail="对话绑定的角色已不存在，无法保存附件")

    try:
        raw = attachments.decode_attachment(payload.content_base64, limit=MAX_ATTACHMENT_BYTES)
        workspace = Path(resolve_workspace(conversation, role))
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
                "agent": prepared.agent.name,
                "role": prepared.role.name,
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
