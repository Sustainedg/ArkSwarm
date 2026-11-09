from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import SessionLocal, get_session
from ..models import AgentInstance, Conversation, Message, Role
from ..runtime import registry
from ..runtime.service import (
    ConfigurationError,
    consume_run,
    new_conversation,
    prepare_run,
    regenerate_last,
)
from ..schemas import ConversationCreate, ConversationRead, ConversationUpdate, MessageRead, SendMessage
from .sse import sse_stream

router = APIRouter(prefix="/conversations", tags=["conversations"])


async def _conversation_read(conversation: Conversation, session: AsyncSession, count: int = 0) -> ConversationRead:
    role = await session.get(Role, conversation.role_id)
    agent = await session.get(AgentInstance, conversation.agent_id)
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
    items = [(await _conversation_read(c, session, counts.get(c.id, 0))).model_dump() for c in rows]
    return {"items": items, "active_runs": registry.list()}


@router.post("", status_code=201)
async def create_conversation(
    payload: ConversationCreate, session: AsyncSession = Depends(get_session)
) -> dict:
    role = await session.get(Role, payload.role_id)
    if role is None:
        raise HTTPException(status_code=422, detail="指定的 role_id 不存在")
    conversation = new_conversation(role, payload.title, payload.workspace_dir)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return (await _conversation_read(conversation, session, 0)).model_dump()


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
    return {
        "conversation": (await _conversation_read(conversation, session, len(messages))).model_dump(),
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
    return (await _conversation_read(conversation, session)).model_dump()


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    registry.cancel_for_conversation(conversation_id)
    for message in (
        await session.execute(select(Message).where(Message.conversation_id == conversation_id))
    ).scalars().all():
        await session.delete(message)
    await session.delete(conversation)
    await session.commit()
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
