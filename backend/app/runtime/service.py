"""把一个 HTTP 请求变成一次真实的智能体运行，并把结果落库。"""
from __future__ import annotations

import asyncio
import logging
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
from ..credentials import with_notice
from ..db import SessionLocal
from ..models import AgentProfile, Conversation, Gateway, Message
from ..settings import get_settings
from .credentials import decrypt_for_profile
from .gateway import bind_gateway
from .manager import ActiveRun, registry
from .mcp import prepare_mcp_home
from .runner import stream_run
from .skills import prepare_skill_home

MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_CHARS = 40_000
DEFAULT_TITLE = "新对话"

logger = logging.getLogger(__name__)


@dataclass
class PreparedRun:
    conversation: Conversation
    profile: AgentProfile
    gateway: Gateway | None
    definition: AgentDefinition
    ctx: AgentContext
    run: ActiveRun
    user_message: Message
    assistant_message: Message
    #: 技能物化过程中的说明/告警，运行开始时以 log 事件暴露
    skill_warnings: list[str] = field(default_factory=list)
    #: MCP 物化过程中的说明/告警，同为 log 事件
    mcp_warnings: list[str] = field(default_factory=list)


class ConfigurationError(Exception):
    pass


async def _load(session: AsyncSession, model: Any, pk: str) -> Any:
    entity = await session.get(model, pk)
    if entity is None:
        raise ConfigurationError(f"找不到 {model.__name__}：{pk}")
    return entity


def resolve_workspace(conversation: Conversation) -> str:
    """对话的工作目录：建对话时指定，没指定就按对话 ID 分配一个独立沙箱。"""
    settings = get_settings()
    raw = conversation.workspace_dir
    base = Path(raw).expanduser() if raw else settings.workspaces_root / conversation.id
    try:
        base = base.resolve()
        base.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(f"工作目录不可用：{base}（{exc}）") from exc
    return str(base)


def resolve_runtime(
    definition: AgentDefinition,
    profile: AgentProfile,
    gateway: Gateway | None,
    model_override: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """解析一次运行要用的网关地址 / API 密钥 / 鉴权方式 / 默认模型。

    地址、密钥与鉴权方式由 ``bind_gateway`` 统一决定（只有自定义网关才注入，
    官方链路与「没选网关」同义、由内核按自己的配置处理）。

    模型优先级：这条对话单独指定的模型 → 智能体的默认模型 → 内核默认模型。
    """
    binding = bind_gateway(definition, gateway)
    model = model_override or profile.model or definition.resolve_model(None)
    return binding.url, binding.api_key, binding.auth_style, model


async def build_context(
    session: AsyncSession,
    definition: AgentDefinition,
    profile: AgentProfile,
    gateway: Gateway | None,
    conversation: Conversation,
    prompt: str,
    history: list[dict[str, str]],
    workspace: str,
) -> AgentContext:
    gateway_url, api_key, auth_style, model = resolve_runtime(
        definition, profile, gateway, conversation.model
    )
    options = definition.coerce_options(profile.options or {})
    if model:
        options.setdefault("model", model)
    # 凭据：本地加密保存、运行时解密后按环境变量注入（见 app.credentials），永远不进提示词。
    credentials = await decrypt_for_profile(session, profile.id)
    return AgentContext(
        agent_id=profile.id,
        agent_name=profile.name,
        options=options,
        gateway_url=gateway_url,
        api_key=api_key,
        auth_style=auth_style,
        model=model,
        system_prompt=with_notice(profile.system_prompt or None, credentials),
        credentials=credentials,
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


async def load_profile(
    session: AsyncSession, profile_id: str
) -> tuple[AgentProfile, Gateway | None, AgentDefinition]:
    profile = await _load(session, AgentProfile, profile_id)
    if profile.deleted_at is not None:
        raise ConfigurationError(f"智能体「{profile.name}」已删除")
    if not profile.enabled:
        raise ConfigurationError(f"智能体「{profile.name}」已停用")
    gateway = await session.get(Gateway, profile.gateway_id) if profile.gateway_id else None
    try:
        definition = get_agent(profile.type_id)
    except KeyError as exc:
        raise ConfigurationError(str(exc)) from None
    return profile, gateway, definition


async def pick_default_profile(session: AsyncSession) -> AgentProfile | None:
    """新建对话时「不问就用它」的智能体，按顺序挑第一个命中的：

    1. 点过五角星的默认智能体（`is_default`）；
    2. 全场只有一个启用的智能体时，就是它（刚装好、还没点过星的那会儿）；
    3. 都没有 → None，由调用方给出「去智能体页点个星」的中文提示。
    """
    active = (
        await session.execute(
            select(AgentProfile)
            .where(AgentProfile.deleted_at.is_(None), AgentProfile.enabled == True)  # noqa: E712
            .order_by(AgentProfile.created_at)
        )
    ).scalars().all()
    for profile in active:
        if profile.is_default:
            return profile
    return active[0] if len(active) == 1 else None


async def prepare_run(session: AsyncSession, conversation_id: str, payload: Any) -> PreparedRun:
    conversation = await _load(session, Conversation, conversation_id)
    profile, gateway, definition = await load_profile(session, conversation.agent_profile_id)

    workspace = payload.workspace_dir or resolve_workspace(conversation)
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
    ctx = await build_context(session, definition, profile, gateway, conversation, content, history, workspace)
    skill_warnings = await prepare_skill_home(session, definition, profile, conversation, ctx)
    mcp_warnings = await prepare_mcp_home(session, definition, profile, conversation, ctx)
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

    run = registry.register(conversation.id, profile.name)
    run.user_message_id = user_message.id
    run.assistant_message_id = assistant_message.id
    return PreparedRun(
        conversation=conversation,
        profile=profile,
        gateway=gateway,
        definition=definition,
        ctx=ctx,
        run=run,
        user_message=user_message,
        assistant_message=assistant_message,
        skill_warnings=skill_warnings,
        mcp_warnings=mcp_warnings,
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


async def _execute_prepared(prepared: PreparedRun) -> None:
    """在后台把一轮运行跑完，与 HTTP 连接解耦。

    浏览器刷新 / 关页面只会让 SSE 订阅者离开，**不能**终止这次运行：
    事件仍写进 ``ActiveRun`` 的缓冲与数据库，重新打开对话（或另一台设备）再订阅
    就能从缓冲重放，接着看实时输出。
    """
    run = prepared.run
    try:
        # 请求作用域的 session 在响应返回后就关闭了，后台运行必须自己开一个。
        async with SessionLocal() as session:
            conversation = await session.get(Conversation, prepared.conversation.id)
            assistant = await session.get(Message, prepared.assistant_message.id)
            if conversation is not None:
                prepared.conversation = conversation
            if assistant is not None:
                prepared.assistant_message = assistant
            async for event in consume_run(session, prepared):
                run.publish(event.to_dict())
    except asyncio.CancelledError:
        # 服务关闭：consume_run 的 finally 已经落库并登记结束，这里只补齐事件流。
        run.publish(error("运行已中断").to_dict())
        run.publish(run_finished("cancelled").to_dict())
        registry.finish(run, "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - 后台任务绝不能静默消失
        run.publish(error(str(exc)).to_dict())
        run.publish(run_finished("error", detail=str(exc)).to_dict())
        registry.finish(run, "error")
        # 运行还没进入 consume_run 就失败时，占位消息会一直停在 streaming，补一次终态。
        try:
            async with SessionLocal() as session:
                assistant = await session.get(Message, prepared.assistant_message.id)
                if assistant is not None and assistant.status == "streaming":
                    assistant.status = "error"
                    assistant.error = str(exc)
                    await session.commit()
        except Exception:  # noqa: BLE001 - 收尾失败不能掩盖上面已经上报的错误
            logger.warning("运行失败后补写助手消息终态失败", exc_info=True)
    finally:
        run.close()


def start_run(prepared: PreparedRun) -> None:
    """把已准备好的一轮运行交给后台任务执行（立即返回，SSE 只负责订阅）。

    ``turn.started`` 是框架自有的传输层事件：在启动后台任务前**同步**写进缓冲，
    保证任何订阅者（包括紧接着打开的另一台设备）都能先看到它。
    """
    run = prepared.run
    run.publish(
        {
            "type": "turn.started",
            "run_id": run.id,
            "conversation_id": prepared.conversation.id,
            "user_message_id": run.user_message_id,
            "assistant_message_id": run.assistant_message_id,
            "agent": prepared.profile.name,
        }
    )
    run.task = asyncio.create_task(_execute_prepared(prepared))


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
    for warning in prepared.mcp_warnings:
        yield log(f"MCP：{warning}", "warn")

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
                status = "error"
                # 适配器把错误文案放在事件的 ``text`` 上（``events.error()``）；只有个别
                # 适配器会额外塞进 payload，所以两处都看，别丢掉真正的原因。
                error_text = event.text or event.payload.get("message") or "智能体返回了错误"
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
        # 先落库再登记结束：重连方看到 active_run 消失时，数据库里一定已经是最终消息，
        # 不会出现「运行没了、消息还停在 streaming」的空窗。
        registry.finish(prepared.run, status)


async def regenerate_last(session: AsyncSession, conversation_id: str) -> PreparedRun:
    """用当前智能体配置重跑最近一轮用户消息。"""
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

    profile, gateway, definition = await load_profile(session, conversation.agent_profile_id)
    workspace = resolve_workspace(conversation)
    history_rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id, Message.created_at < last_user.created_at)
            .order_by(Message.created_at)
        )
    ).scalars().all()
    history = _clip_history(list(history_rows))
    ctx = await build_context(
        session, definition, profile, gateway, conversation, last_user.content, history, workspace
    )
    skill_warnings = await prepare_skill_home(session, definition, profile, conversation, ctx)
    mcp_warnings = await prepare_mcp_home(session, definition, profile, conversation, ctx)
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

    run = registry.register(conversation.id, profile.name)
    run.user_message_id = last_user.id
    run.assistant_message_id = assistant_message.id
    return PreparedRun(
        conversation=conversation,
        profile=profile,
        gateway=gateway,
        definition=definition,
        ctx=ctx,
        run=run,
        user_message=last_user,
        assistant_message=assistant_message,
        skill_warnings=skill_warnings,
        mcp_warnings=mcp_warnings,
    )


def new_conversation(profile: AgentProfile, title: str | None, workspace_dir: str | None) -> Conversation:
    return Conversation(
        title=(title or DEFAULT_TITLE).strip() or DEFAULT_TITLE,
        agent_profile_id=profile.id,
        workspace_dir=workspace_dir,
    )


def ephemeral_id() -> str:
    return uuid.uuid4().hex
