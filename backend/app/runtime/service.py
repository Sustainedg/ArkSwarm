"""Turns an HTTP request into a live agent run and persists the outcome."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, EventType, get_agent
from ..agents.events import AgentEvent, error, run_finished
from ..models import AgentInstance, Conversation, Message, Role
from ..security import decrypt
from ..settings import get_settings
from .manager import ActiveRun, registry
from .runner import stream_run

MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_CHARS = 40_000
DEFAULT_TITLE = "New conversation"


@dataclass
class PreparedRun:
    conversation: Conversation
    role: Role
    agent: AgentInstance
    definition: AgentDefinition
    ctx: AgentContext
    run: ActiveRun
    user_message: Message
    assistant_message: Message
    ordering: list[dict[str, Any]] = field(default_factory=list)


class ConfigurationError(Exception):
    pass


async def _load(session: AsyncSession, model: Any, pk: str) -> Any:
    entity = await session.get(model, pk)
    if entity is None:
        raise ConfigurationError(f"{model.__name__} {pk} not found")
    return entity


def resolve_workspace(conversation: Conversation, role: Role) -> str:
    settings = get_settings()
    raw = conversation.workspace_dir or role.workspace_dir
    base = Path(raw).expanduser() if raw else settings.workspaces_root / conversation.id
    base = base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def build_context(
    definition: AgentDefinition,
    agent: AgentInstance,
    role: Role,
    conversation: Conversation,
    prompt: str,
    history: list[dict[str, str]],
    workspace: str,
) -> AgentContext:
    options = definition.coerce_options(agent.options or {})
    # role-level overrides win over the agent defaults
    options = {**options, **(role.parameters or {})}
    if not options.get("model") and agent.model:
        options["model"] = agent.model

    model = role.parameters.get("model") if role.parameters else None
    model = model or agent.model or definition.default_model
    gateway = (role.parameters or {}).get("gateway_url") or agent.gateway_url or definition.default_gateway

    return AgentContext(
        agent_id=agent.id,
        agent_name=agent.name,
        options=options,
        gateway_url=gateway,
        api_key=decrypt(agent.api_key_encrypted),
        model=model,
        role_name=role.name,
        system_prompt=role.system_prompt or None,
        workspace_dir=workspace,
        session_id=conversation.session_id,
        history=history,
        prompt=prompt,
    )


def supports_sessions(definition: AgentDefinition, ctx: AgentContext) -> bool:
    if not definition.capabilities.sessions:
        return False
    probe = getattr(definition, "resume_supported", None)
    return bool(probe(ctx)) if callable(probe) else True


def _clip_history(rows: list[Message]) -> list[dict[str, str]]:
    picked: list[dict[str, str]] = []
    budget = MAX_HISTORY_CHARS
    for row in reversed(rows[-MAX_HISTORY_MESSAGES:]):
        if row.role not in ("user", "assistant") or not row.content:
            continue
        budget -= len(row.content)
        if budget < 0:
            break
        picked.append({"role": row.role, "content": row.content})
    return list(reversed(picked))


def transcript_prompt(system_prompt: str | None, history: list[dict[str, str]], prompt: str) -> str:
    """Used by agents with neither a system-prompt flag nor session resume."""
    lines = []
    if system_prompt:
        lines.append(f"[system]\n{system_prompt}")
    for turn in history:
        label = "user" if turn["role"] == "user" else "assistant"
        lines.append(f"[{label}]\n{turn['content']}")
    lines.append(f"[user]\n{prompt}\n\n[assistant]")
    return "\n\n".join(lines)


async def prepare_run(session: AsyncSession, conversation_id: str, payload: Any) -> PreparedRun:
    conversation = await _load(session, Conversation, conversation_id)
    role = await _load(session, Role, conversation.role_id)
    agent = await _load(session, AgentInstance, conversation.agent_id)
    if not agent.enabled:
        raise ConfigurationError(f"agent '{agent.name}' is disabled")

    try:
        definition = get_agent(agent.type_id)
    except KeyError as exc:
        raise ConfigurationError(str(exc)) from None

    workspace = payload.workspace_dir or resolve_workspace(conversation, role)
    if payload.reset_session:
        conversation.session_id = None

    existing = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at)
        )
    ).scalars().all()

    user_message = Message(conversation_id=conversation.id, role="user", content=payload.content)
    session.add(user_message)

    history = _clip_history(list(existing))
    ctx = build_context(definition, agent, role, conversation, payload.content, history, workspace)
    if not supports_sessions(definition, ctx):
        ctx.session_id = None
        if not definition.capabilities.native_system_prompt:
            ctx.prompt = transcript_prompt(ctx.system_prompt, history, payload.content)
            ctx.system_prompt = None

    assistant_message = Message(
        conversation_id=conversation.id, role="assistant", content="", status="streaming"
    )
    session.add(assistant_message)

    if conversation.title in (DEFAULT_TITLE, "") and payload.content.strip():
        conversation.title = payload.content.strip().splitlines()[0][:80]
    conversation.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(user_message)
    await session.refresh(assistant_message)

    run = registry.register(conversation.id, agent.name)
    return PreparedRun(
        conversation=conversation,
        role=role,
        agent=agent,
        definition=definition,
        ctx=ctx,
        run=run,
        user_message=user_message,
        assistant_message=assistant_message,
    )


async def consume_run(session: AsyncSession, prepared: PreparedRun) -> AsyncIterator[AgentEvent]:
    """Execute the run, yielding events and persisting the final state."""
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_parts: list[dict[str, Any]] = []
    usage_totals: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    status = "complete"
    error_text: str | None = None

    try:
        async for event in stream_run(prepared.definition, prepared.ctx, prepared.run):
            if event.type is EventType.MESSAGE_DELTA and event.text:
                text_parts.append(event.text)
            elif event.type is EventType.THINKING_DELTA and event.text:
                thinking_parts.append(event.text)
            elif event.type is EventType.TOOL_CALL:
                tool_parts.append(
                    {"kind": "tool_call", "id": event.call_id, "name": event.name, **(event.payload or {})}
                )
            elif event.type is EventType.TOOL_RESULT:
                tool_parts.append(
                    {
                        "kind": "tool_result",
                        "id": event.call_id,
                        "output": event.payload.get("output"),
                        "is_error": event.payload.get("is_error", False),
                    }
                )
            elif event.type is EventType.USAGE:
                usage_totals.update({k: v for k, v in (event.payload or {}).items() if v is not None})
            elif event.type is EventType.SESSION:
                prepared.conversation.session_id = event.payload.get("session_id")
                session.add(prepared.conversation)
            elif event.type is EventType.ERROR:
                error_text = event.text or "agent error"
            elif event.type is EventType.RUN_FINISHED:
                extra = dict(event.payload or {})
                if extra.get("status") in ("error", "cancelled"):
                    status = extra["status"]

            yield event
    except Exception as exc:  # noqa: BLE001 - never let a run kill the stream silently
        error_text = str(exc)
        status = "error"
        yield error(str(exc))
        yield run_finished("error", detail=str(exc))
    finally:
        registry.finish(prepared.run, status)
        message = prepared.assistant_message
        message.content = "".join(text_parts)
        parts: list[dict[str, Any]] = []
        if thinking_parts:
            parts.append({"kind": "thinking", "text": "".join(thinking_parts)})
        parts.extend(tool_parts)
        message.parts = parts
        message.usage = usage_totals
        message.error = error_text
        message.status = "error" if status == "error" else ("interrupted" if status == "cancelled" else "complete")
        prepared.conversation.updated_at = datetime.now(timezone.utc)
        session.add(message)
        session.add(prepared.conversation)
        try:
            await session.commit()
        except Exception:  # noqa: BLE001 - keep the client stream alive
            await session.rollback()


async def regenerate_last(session: AsyncSession, conversation_id: str) -> PreparedRun:
    """Re-run the most recent user turn against the current role configuration."""
    conversation = await _load(session, Conversation, conversation_id)
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
        )
    ).scalars().all()
    last_user = next((m for m in rows if m.role == "user"), None)
    if last_user is None:
        raise ConfigurationError("no user message to regenerate from")

    # drop everything after the last user turn, then replay it
    stale = [m for m in rows if m.created_at >= last_user.created_at and m.role != "user"]
    for message in stale:
        await session.delete(message)
    await session.commit()

    role = await _load(session, Role, conversation.role_id)
    agent = await _load(session, AgentInstance, conversation.agent_id)
    definition = get_agent(agent.type_id)
    workspace = resolve_workspace(conversation, role)
    history_rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.created_at < last_user.created_at)
            .order_by(Message.created_at)
        )
    ).scalars().all()
    history = _clip_history(list(history_rows))
    ctx = build_context(definition, agent, role, conversation, last_user.content, history, workspace)
    if not supports_sessions(definition, ctx):
        ctx.session_id = None
        if not definition.capabilities.native_system_prompt:
            ctx.prompt = transcript_prompt(ctx.system_prompt, history, last_user.content)
            ctx.system_prompt = None

    assistant_message = Message(
        conversation_id=conversation.id, role="assistant", content="", status="streaming"
    )
    session.add(assistant_message)
    await session.commit()
    await session.refresh(assistant_message)

    run = registry.register(conversation.id, agent.name)
    return PreparedRun(
        conversation=conversation,
        role=role,
        agent=agent,
        definition=definition,
        ctx=ctx,
        run=run,
        user_message=last_user,
        assistant_message=assistant_message,
    )


def new_conversation(role: Role, title: str | None, workspace_dir: str | None) -> Conversation:
    return Conversation(
        title=(title or DEFAULT_TITLE).strip() or DEFAULT_TITLE,
        role_id=role.id,
        agent_id=role.agent_id,
        workspace_dir=workspace_dir,
    )


def ephemeral_id() -> str:
    return uuid.uuid4().hex
