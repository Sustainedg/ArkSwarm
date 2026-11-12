"""把一个 HTTP 请求变成一次真实的智能体运行，并把结果落库。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, EventType, get_agent
from ..agents.events import AgentEvent, error, log, run_finished
from ..attachments import AttachmentError, attachment_relative_path, compose_with_attachments
from ..models import AgentInstance, Conversation, Message, Role
from ..security import decrypt
from ..settings import get_settings
from .manager import ActiveRun, registry
from .runner import stream_run
from .skills import prepare_skill_home

MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_CHARS = 40_000
DEFAULT_TITLE = "新对话"


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
    #: 技能物化过程中的说明/告警，运行开始时以 log 事件暴露
    skill_warnings: list[str] = field(default_factory=list)


class ConfigurationError(Exception):
    pass


async def _load(session: AsyncSession, model: Any, pk: str) -> Any:
    entity = await session.get(model, pk)
    if entity is None:
        raise ConfigurationError(f"找不到 {model.__name__}：{pk}")
    return entity


def resolve_workspace(conversation: Conversation, role: Role) -> str:
    settings = get_settings()
    raw = conversation.workspace_dir or role.workspace_dir
    base = Path(raw).expanduser() if raw else settings.workspaces_root / conversation.id
    try:
        base = base.resolve()
        base.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(f"工作目录不可用：{base}（{exc}）") from exc
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
    # 角色级覆盖优先于智能体实例上的默认值
    options = {**options, **(role.parameters or {})}
    if not options.get("model") and agent.model:
        options["model"] = definition.resolve_model(agent.model)

    # 未被用户显式修改的网关/模型，回落到适配器读到的本机真实配置。
    model = role.parameters.get("model") if role.parameters else None
    model = model or definition.resolve_model(agent.model)
    gateway = (role.parameters or {}).get("gateway_url") or definition.resolve_gateway(agent.gateway_url)

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


def resolve_attachment(relative: str, workspace: str) -> str:
    """校验附件确实在本次运行的工作目录内，返回规范化的相对路径。

    实现与「📎 附件」提示块都在 :mod:`app.attachments`（协作域共用同一份），
    这里只把异常换成运行层的 ``ConfigurationError``。
    """
    try:
        return attachment_relative_path(relative, workspace)
    except AttachmentError as exc:
        raise ConfigurationError(str(exc)) from None


def compose_user_content(content: str, attachments: list[str] | None, workspace: str) -> str:
    """把附件写进用户提示词：文件已经落在工作目录里，这里明确点名让智能体去读。

    这样做的好处是与内核无关 —— 只要能读工作目录的文件（Claude Code / Codex / Gemini CLI /
    custom-cli 都行），附件就生效，不需要适配器或事件协议做任何改动。
    """
    paths = [resolve_attachment(item, workspace) for item in attachments or []]
    return compose_with_attachments(content, paths)


def transcript_prompt(system_prompt: str | None, history: list[dict[str, str]], prompt: str) -> str:
    """用于既不支持系统提示词参数、也不支持会话续接的智能体。"""
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
        raise ConfigurationError(f"智能体「{agent.name}」已停用")

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

    content = compose_user_content(
        payload.content, getattr(payload, "attachments", None), workspace
    )
    user_message = Message(conversation_id=conversation.id, role="user", content=content)
    session.add(user_message)

    history = _clip_history(list(existing))
    ctx = build_context(definition, agent, role, conversation, content, history, workspace)
    skill_warnings = await prepare_skill_home(session, definition, agent, role, conversation, ctx)
    if not supports_sessions(definition, ctx):
        ctx.session_id = None
        if not definition.capabilities.native_system_prompt:
            ctx.prompt = transcript_prompt(ctx.system_prompt, history, content)
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
        skill_warnings=skill_warnings,
    )


def _merge_part(parts: list[dict[str, Any]], kind: str, text: str) -> None:
    """把增量文本并入时间线：与上一段同类则续写，否则另起一段。

    这样落库的 ``parts`` 会保留「思考 → 正文 → 工具调用 → 工具结果 → 正文 …」
    的真实先后顺序，而不是按类型归组。
    """
    if parts and parts[-1].get("kind") == kind:
        parts[-1]["text"] = (parts[-1].get("text") or "") + text
    else:
        parts.append({"kind": kind, "text": text})


async def consume_run(session: AsyncSession, prepared: PreparedRun) -> AsyncIterator[AgentEvent]:
    """执行运行、产出事件，并把最终状态写入数据库。"""
    text_parts: list[str] = []
    parts: list[dict[str, Any]] = []
    usage_totals: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    status = "complete"
    error_text: str | None = None

    for warning in prepared.skill_warnings:
        yield log(f"技能：{warning}", "warn")

    try:
        async for event in stream_run(prepared.definition, prepared.ctx, prepared.run):
            if event.type is EventType.MESSAGE_DELTA and event.text:
                text_parts.append(event.text)
                _merge_part(parts, "text", event.text)
            elif event.type is EventType.THINKING_DELTA and event.text:
                _merge_part(parts, "thinking", event.text)
            elif event.type is EventType.TOOL_CALL:
                parts.append(
                    {"kind": "tool_call", "id": event.call_id, "name": event.name, **(event.payload or {})}
                )
            elif event.type is EventType.TOOL_RESULT:
                parts.append(
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
                error_text = event.text or "智能体返回了错误"
            elif event.type is EventType.RUN_FINISHED:
                extra = dict(event.payload or {})
                if extra.get("status") in ("error", "cancelled"):
                    status = extra["status"]

            yield event
    except Exception as exc:  # noqa: BLE001 - 绝不让异常静默地中断整条事件流
        error_text = str(exc)
        status = "error"
        yield error(str(exc))
        yield run_finished("error", detail=str(exc))
    finally:
        registry.finish(prepared.run, status)
        message = prepared.assistant_message
        message.content = "".join(text_parts)
        message.parts = parts
        message.usage = usage_totals
        message.error = error_text
        message.status = "error" if status == "error" else ("interrupted" if status == "cancelled" else "complete")
        prepared.conversation.updated_at = datetime.now(timezone.utc)
        session.add(message)
        session.add(prepared.conversation)
        try:
            await session.commit()
        except Exception:  # noqa: BLE001 - 落库失败也要保持客户端事件流不断
            await session.rollback()


async def regenerate_last(session: AsyncSession, conversation_id: str) -> PreparedRun:
    """用当前角色配置重跑最近一轮用户消息。"""
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
        raise ConfigurationError("没有可用于重新生成的用户消息")

    # 丢弃最后一轮用户消息之后的内容，然后重新重放这一轮
    stale = [m for m in rows if m.created_at >= last_user.created_at and m.role != "user"]
    for message in stale:
        await session.delete(message)
    await session.commit()

    role = await _load(session, Role, conversation.role_id)
    agent = await _load(session, AgentInstance, conversation.agent_id)
    try:
        definition = get_agent(agent.type_id)
    except KeyError as exc:
        # 适配器被下架后，历史智能体实例仍留在库里：这里要给一句可读的中文提示，
        # 而不是让 KeyError 冒成 500（铁律 11）。
        raise ConfigurationError(str(exc)) from None
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
    skill_warnings = await prepare_skill_home(session, definition, agent, role, conversation, ctx)
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
        skill_warnings=skill_warnings,
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
