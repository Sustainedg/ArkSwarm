from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import attachments
from ..agents import get_agent
from ..db import get_session
from ..models import AgentProfile, Conversation, Gateway, Message
from ..runtime import registry
from ..runtime import skills as skill_service
from ..runtime import titles
from ..runtime.service import (
    ConfigurationError,
    new_conversation,
    pick_default_profile,
    prepare_run,
    regenerate_last,
    resolve_workspace,
    start_run,
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
    unread: bool = False,
) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        agent_profile_id=conversation.agent_profile_id,
        session_id=conversation.session_id,
        workspace_dir=conversation.workspace_dir,
        model=conversation.model,
        archived=conversation.archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        agent_name=profile.name if profile else None,
        agent_avatar=profile.avatar if profile else None,
        agent_type_id=profile.type_id if profile else None,
        message_count=count,
        unread=unread,
    )


def _as_utc(value: datetime) -> datetime:
    """SQLite 不存时区：比大小前先统一成 UTC，别拿 aware 和 naive 直接比。"""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_unread(conversation: Conversation, last: Message | None) -> bool:
    """「AI 回复完了、但用户还没看」= 最后一条是已完成的助手消息，且晚于最后已读时间。

    生成中的 ``streaming`` 占位不算 —— 那时列表行显示的是「正在回复」的动画。
    升级前的旧对话没有 ``last_read_at``，一律当已读。
    """
    if last is None or last.role != "assistant" or last.status == "streaming":
        return False
    if conversation.last_read_at is None:
        return False
    return _as_utc(last.created_at) > _as_utc(conversation.last_read_at)


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
    # 每条对话的最后一条消息：一次批量取回，别为未读判断逐条查（N+1）。
    # 先按对话求 max(created_at)，再把时间相等的那几行取出来。
    latest = {}
    if rows:
        latest_at = (
            select(Message.conversation_id, func.max(Message.created_at).label("latest_at"))
            .where(Message.conversation_id.in_([c.id for c in rows]))
            .group_by(Message.conversation_id)
            .subquery()
        )
        latest_rows = (
            await session.execute(
                select(Message).join(
                    latest_at,
                    (Message.conversation_id == latest_at.c.conversation_id)
                    & (Message.created_at == latest_at.c.latest_at),
                )
            )
        ).scalars().all()
        latest = {message.conversation_id: message for message in latest_rows}
    # 智能体批量取一次：逐个 session.get 会让一次列表请求打出 N 条查询。
    profile_ids = {c.agent_profile_id for c in rows}
    profiles = {
        row.id: row
        for row in (
            await session.execute(select(AgentProfile).where(AgentProfile.id.in_(profile_ids)))
        ).scalars()
    }
    items = [
        _conversation_read(
            c,
            profiles.get(c.agent_profile_id),
            counts.get(c.id, 0),
            _is_unread(c, latest.get(c.id)),
        ).model_dump()
        for c in rows
    ]
    return {"items": items, "active_runs": registry.list()}


@router.post("/{conversation_id}/read", status_code=204)
async def mark_conversation_read(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    """把这条对话标记为已读（打开 / 正在看时由前端调用）。

    只改 ``last_read_at``，**不动** ``updated_at``：标已读不该让对话跳到列表最前面。
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    conversation.last_read_at = datetime.now(timezone.utc)
    session.add(conversation)
    await session.commit()
    return Response(status_code=204)


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
    agent_changed = False
    if data.get("agent_profile_id") and data["agent_profile_id"] != conversation.agent_profile_id:
        profile = await session.get(AgentProfile, data.pop("agent_profile_id"))
        if profile is None or profile.deleted_at is not None:
            raise HTTPException(status_code=422, detail="指定的智能体不存在")
        conversation.agent_profile_id = profile.id
        scope_changed = True
        agent_changed = True
    if "workspace_dir" in data:
        workspace = (data.pop("workspace_dir") or "").strip()
        if workspace != (conversation.workspace_dir or ""):
            scope_changed = True
        conversation.workspace_dir = workspace or None
    if "model" in data:
        # 换模型不清会话记忆（同一个内核会话可以换模型继续跑），只是下一轮用它。
        # 自定义网关必须守着白名单这条服务端不变量；官方默认链路允许直接填模型名。
        model = (data.pop("model") or "").strip()
        if model:
            profile = await session.get(AgentProfile, conversation.agent_profile_id)
            gateway = (
                await session.get(Gateway, profile.gateway_id)
                if profile is not None and profile.gateway_id
                else None
            )
            if gateway is not None and model not in (gateway.selected_models or []):
                raise HTTPException(status_code=422, detail="模型不在该网关的白名单里")
        conversation.model = model or None
    if scope_changed and registry.active_for(conversation_id):
        raise HTTPException(status_code=409, detail="这条对话正在运行，先停止或等它结束再改")
    if scope_changed:
        conversation.session_id = None
    if agent_changed:
        # 换智能体后模型要回到新智能体的默认（旧模型可能属于另一个网关的白名单）。
        conversation.model = None

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


@router.get("/{conversation_id}/files/{rel_path:path}")
async def get_conversation_file(
    conversation_id: str, rel_path: str, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    """把工作目录里的一个文件回给浏览器看（附件点开预览、智能体产出的图片等）。

    只读，且**仅限该对话工作目录内部**（`../` 穿越、目录、目录外软链一律拒绝）。附件本就是
    工作目录里的普通文件，所以「预览我上传的图」与「看智能体生成的图」共用这一个出口 ——
    这也是聊天里嵌图能真正显示出来的前提（浏览器取不到工作目录里的本地路径）。
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    try:
        workspace = Path(resolve_workspace(conversation))
        target = attachments.resolve_workspace_file(rel_path, workspace)
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return FileResponse(
        target,
        media_type=attachments.guess_media_type(target.name),
        # 内容会随对话进展变化（同名路径可能被重写），只让浏览器短暂缓存，避免看到旧图。
        headers={"Cache-Control": "private, max-age=30"},
    )


def _sse_response(source: AsyncIterator[dict]) -> StreamingResponse:
    return StreamingResponse(
        sse_stream(source),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _stream_prepared(prepared) -> StreamingResponse:
    """只负责订阅后台运行的事件。

    运行的执行与 HTTP 连接解耦（``service.start_run``）：客户端刷新或关页面时
    只是这个订阅者离开，运行继续跑完并落库；重新打开对话会走
    ``GET /conversations/{id}/stream`` 从缓冲重放，再接着实时输出。
    """

    async def event_source() -> AsyncIterator[dict]:
        async for event in prepared.run.subscribe():
            yield event

    return _sse_response(event_source())


@router.get("/{conversation_id}/stream")
async def stream_active_run(conversation_id: str) -> StreamingResponse:
    """订阅一条对话当前正在运行的那一轮（刷新 / 另一台设备实时围观用）。"""
    run = registry.active_for(conversation_id)
    if run is None:
        raise HTTPException(status_code=404, detail="这条对话当前没有正在运行的任务")

    async def event_source() -> AsyncIterator[dict]:
        async for event in run.subscribe():
            yield event

    return _sse_response(event_source())


@router.get("/{conversation_id}/models")
async def conversation_models(
    conversation_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """这条对话可选的模型清单。

    有自定义网关时只能用网关白名单里的模型；官方默认时返回空清单，由前端
    允许直接填写模型名（与「智能体」页的默认模型输入一致）。
    """
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    profile = await session.get(AgentProfile, conversation.agent_profile_id)
    gateway = (
        await session.get(Gateway, profile.gateway_id)
        if profile is not None and profile.gateway_id
        else None
    )
    default_model = profile.model if profile is not None else None
    if not default_model and profile is not None:
        try:
            default_model = get_agent(profile.type_id).resolve_model(None)
        except KeyError:
            default_model = None
    return {
        "current": conversation.model,
        "default": default_model,
        "gateway": gateway.name if gateway is not None else None,
        "models": list(gateway.selected_models) if gateway is not None else [],
    }


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str, payload: SendMessage, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    try:
        prepared = await prepare_run(session, conversation_id, payload)
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    start_run(prepared)
    # 这是这条对话的第一句话：丢一个后台任务用「设置」里指定的智能体把标题概括得更准
    # （临时标题已经在 prepare_run 里按首行写好，真跑不出来也不影响使用）。
    if prepared.title_pending:
        titles.schedule(prepared.conversation.id, prepared.conversation.title, prepared.user_message.content)
    return _stream_prepared(prepared)


@router.post("/{conversation_id}/regenerate")
async def regenerate(conversation_id: str, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    try:
        prepared = await regenerate_last(session, conversation_id)
    except ConfigurationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    start_run(prepared)
    return _stream_prepared(prepared)


@router.post("/{conversation_id}/cancel")
async def cancel_run(conversation_id: str) -> dict:
    cancelled = registry.cancel_for_conversation(conversation_id)
    return {"ok": cancelled}
