"""协作域的 HTTP 接口（薄层：只做参数校验、状态码与序列化）。

编排逻辑在 ``engine.py``（M1 落地），图的判断在 ``graph.py``。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import attachments
from ..db import SessionLocal, get_session
from ..models import AgentProfile, utcnow
from ..transport.sse import sse_stream
from .bus import bus, chat_bus
from . import engine, execution, graph as graph_service, store
from .schemas import (
    GraphSpec,
    RunAttachmentUpload,
    RunCreate,
    RunDetail,
    RunMessage,
    RunRead,
    TeamCreate,
    TeamRead,
    TeamUpdate,
    TeamValidateIn,
)
from .tables import CollabRun, CollabStep, CollabTicket

router = APIRouter(prefix="/collab", tags=["collab"])


def _conflict(exc: store.TeamInUse | store.TeamKeyInUse) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


async def _known_agent_profile_ids(session: AsyncSession) -> set[str]:
    """把「智能体是否还存在」这类事实注入纯函数，校验层不碰数据库。"""
    rows = (await session.execute(select(AgentProfile.id))).all()
    return {row[0] for row in rows}


@router.get("/teams")
async def list_teams(session: AsyncSession = Depends(get_session)) -> dict:
    rows = await store.list_teams(session)
    known = await _known_agent_profile_ids(session)
    return {"items": [store.to_read(row, known) for row in rows]}


@router.post("/teams", status_code=201)
async def create_team(payload: TeamCreate, session: AsyncSession = Depends(get_session)) -> TeamRead:
    team = await store.create_team(session, payload)
    return store.to_read(team, await _known_agent_profile_ids(session))


@router.get("/teams/{team_id}")
async def get_team(team_id: str, session: AsyncSession = Depends(get_session)) -> TeamRead:
    try:
        team = await store.get_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_agent_profile_ids(session))


@router.put("/teams/{team_id}")
async def save_team(
    team_id: str, payload: TeamUpdate, session: AsyncSession = Depends(get_session)
) -> TeamRead:
    try:
        team = await store.save_team(session, team_id, payload)
    except store.TeamKeyInUse as exc:
        raise _conflict(exc) from None
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_agent_profile_ids(session))


@router.post("/teams/{team_id}/validate")
async def validate_team(
    team_id: str, payload: TeamValidateIn | None = None, session: AsyncSession = Depends(get_session)
) -> dict:
    """只校验不保存：编辑器每改一笔就调一次，用来就地提示问题。"""
    try:
        team = await store.get_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None

    body = payload or TeamValidateIn()
    if body.graph is not None:
        spec: GraphSpec = body.graph
    else:
        try:
            spec = graph_service.from_storable(team.graph)
        except ValueError:
            spec = GraphSpec()
    entry_key = body.entry_key if body.entry_key is not None else team.entry_key
    known = await _known_agent_profile_ids(session)
    return graph_service.validate_graph(spec, entry_key, known).model_dump()


@router.post("/teams/{team_id}/duplicate", status_code=201)
async def duplicate_team(team_id: str, session: AsyncSession = Depends(get_session)) -> TeamRead:
    try:
        team = await store.duplicate_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_agent_profile_ids(session))


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(team_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    try:
        await store.delete_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except store.TeamInUse as exc:
        raise _conflict(exc) from None
    return Response(status_code=204)


# --- 运行 -------------------------------------------------------------------


async def _list_run_reads(session: AsyncSession, team_id: str | None) -> list[RunRead]:
    rows = await store.list_runs(session, team_id)
    return [await store.run_read(session, row) for row in rows]


@router.get("/runs")
async def list_runs(team_id: str | None = None, session: AsyncSession = Depends(get_session)) -> dict:
    return {"items": [item.model_dump() for item in await _list_run_reads(session, team_id)]}


@router.get("/active-runs")
async def active_runs(session: AsyncSession = Depends(get_session)) -> dict:
    """本进程里正在跑的协作智能体（工单步骤 + 入口智能体的对话回复）。

    设置页的「正在运行的智能体」要一并显示它们：协作的运行登记在协作域自己这里，
    对话域那份运行表（`GET /api/system/overview` 的 `active_runs`）看不到。
    汇总接口留在各自域的边界上，所以这里由协作域自己给一份，并补上运行标题（界面好认）。
    """
    items = execution.active_runs()
    if items:
        ids = {item["run_id"] for item in items}
        rows = (
            await session.execute(select(CollabRun).where(CollabRun.id.in_(ids)))
        ).scalars().all()
        titles = {row.id: row.title for row in rows}
        for item in items:
            item["title"] = titles.get(item["run_id"])
    return {"items": items}


@router.post("/runs", status_code=201)
async def create_run(payload: RunCreate, session: AsyncSession = Depends(get_session)) -> RunRead:
    try:
        run = await store.create_run(session, payload)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return await store.run_read(session, run)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> RunDetail:
    try:
        run = await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    detail = await store.run_detail(session, run)
    detail.chat_active = chat_bus.is_running(run_id) or execution.is_chat_running(run_id)
    return detail


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    execution.cancel_run(run_id)
    bus.cancel(run_id)
    chat_bus.cancel(run_id)
    try:
        await store.delete_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return Response(status_code=204)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        run = await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    execution.cancel_run(run_id)
    cancelled = bus.cancel(run_id)
    chat_cancelled = chat_bus.cancel(run_id)
    if run.status == "running":
        await store.mark_interrupted(session, run_id, "已停止")
    await store.mark_chat_interrupted(session, run_id, "你停止了这次对话")
    return {"ok": True, "cancelled": cancelled or chat_cancelled}


async def _drive_turn(prepared: engine.PreparedTurn) -> AsyncIterator[dict]:
    """后台推进一个 turn：自己开数据库会话，生命周期与 HTTP 请求无关。"""
    async with SessionLocal() as session:
        run = await session.get(CollabRun, prepared.run.id)
        if run is not None:
            prepared.run = run
        async for event in engine.consume_turn(session, prepared):
            yield event.to_dict()


def _sse(source: AsyncIterator[dict]) -> StreamingResponse:
    """把事件源包成 SSE 响应（心跳与 ``stream.closed`` 由传输层补）。"""
    async def guarded() -> AsyncIterator[str]:
        async for chunk in sse_stream(source):
            yield chunk

    return StreamingResponse(
        guarded(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _stream_turn(prepared: engine.PreparedTurn) -> StreamingResponse:
    """启动后台推进并返回 SSE。

    推进跑在独立任务里，客户端断开只会退订、**不会**打断它；重新打开页面用
    ``GET /runs/{id}/stream`` 订阅同一份实时事件即可续看。
    """
    run_id = prepared.run.id
    bus.start(run_id, lambda: _drive_turn(prepared))
    return _sse(bus.subscribe(run_id))


async def _drive_chat(prepared: engine.PreparedChat) -> AsyncIterator[dict]:
    """后台执行一次 CEO 回复：与主流程使用各自独立的数据库会话。"""
    async with SessionLocal() as session:
        run = await session.get(CollabRun, prepared.run.id)
        if run is not None:
            prepared.run = run
        async for event in engine.consume_chat(session, prepared):
            yield event.to_dict()


def _stream_chat(prepared: engine.PreparedChat) -> StreamingResponse:
    run_id = prepared.run.id
    chat_bus.start(run_id, lambda: _drive_chat(prepared))
    return _sse(chat_bus.subscribe(run_id))


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """订阅一次运行的实时事件：用于断开后重连，或在另一台设备上实时围观。"""
    try:
        await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return _sse(bus.subscribe(run_id))


@router.post("/runs/{run_id}/messages")
async def send_message(
    run_id: str, payload: RunMessage, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    if bus.is_running(run_id):
        raise HTTPException(status_code=409, detail="这次协作正在推进中，请稍候（可直接围观实时输出）")
    if chat_bus.is_running(run_id) or execution.is_chat_running(run_id):
        raise HTTPException(status_code=409, detail="CEO 正在回复你的上一条消息，请稍等它说完")
    try:
        prepared = await engine.prepare_turn(session, run_id, payload.content)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except engine.TurnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_turn(prepared)


@router.get("/runs/{run_id}/chat")
async def list_chat(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """入口智能体对话历史；运行推进期间也可以随时读取。"""
    try:
        await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"items": [item.model_dump() for item in await store.list_chat_messages(session, run_id)]}


@router.get("/runs/{run_id}/chat/stream")
async def stream_chat(run_id: str, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """订阅正在生成的 CEO 回复；断线重连时先补发本轮已产生的事件。"""
    try:
        await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return _sse(chat_bus.subscribe(run_id))


@router.post("/runs/{run_id}/chat")
async def send_chat(
    run_id: str, payload: RunMessage, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    """和入口智能体聊天：**不受**主流程是否正在推进影响。"""
    if not (payload.content or "").strip() and not payload.attachments:
        raise HTTPException(status_code=422, detail="请先输入想对 CEO 说的话，或附上一个文件")
    if chat_bus.is_running(run_id) or execution.is_chat_running(run_id):
        raise HTTPException(status_code=409, detail="CEO 正在回复上一条消息，请稍等它说完")
    try:
        await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    try:
        prepared = await engine.prepare_chat(session, run_id, payload.content, payload.attachments)
    except engine.ChatError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_chat(prepared)


@router.post("/runs/{run_id}/attachments", status_code=201)
async def upload_run_attachment(
    run_id: str, payload: RunAttachmentUpload, session: AsyncSession = Depends(get_session)
) -> dict:
    """把附件存进这次协作的工作目录（`<工作目录>/attachments/`）。

    与对话附件同一套实现（:mod:`app.attachments`）：文件落到工作目录里，发消息时把路径
    写进提示词，因此任何能读工作目录的内核都自动支持。
    """
    try:
        run = await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    try:
        raw = attachments.decode_attachment(payload.content_base64)
        workspace = execution.ensure_workspace(run)
        stored = attachments.store_attachment(workspace, payload.name, raw)
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except execution.StepError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return stored.to_dict()


@router.get("/runs/{run_id}/files/{rel_path:path}")
async def get_run_file(
    run_id: str, rel_path: str, session: AsyncSession = Depends(get_session)
) -> FileResponse:
    """把这次协作工作目录里的一个文件回给浏览器看（附件预览、岗位产出的图片等）。

    只读，且仅限工作目录内部（与对话域同一个 `app.attachments.resolve_workspace_file` 守卫）；
    路径不存在时不创建目录，直接 4xx。
    """
    try:
        run = await store.get_run(session, run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    try:
        target = attachments.resolve_workspace_file(rel_path, execution.workspace_root(run))
    except attachments.AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return FileResponse(
        target,
        media_type=attachments.guess_media_type(target.name),
        headers={"Cache-Control": "private, max-age=30"},
    )


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """从中断处继续（不追加用户消息）。"""
    if bus.is_running(run_id):
        raise HTTPException(status_code=409, detail="这次协作正在推进中，请稍候（可直接围观实时输出）")
    if chat_bus.is_running(run_id) or execution.is_chat_running(run_id):
        raise HTTPException(status_code=409, detail="CEO 正在回复你的上一条消息，请稍等它说完")
    try:
        prepared = await engine.prepare_turn(session, run_id, None)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except engine.TurnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_turn(prepared)


@router.post("/steps/{step_id}/retry")
async def retry_step(step_id: str, session: AsyncSession = Depends(get_session)) -> RunRead:
    """把这一步所在的工单放回队列，随后用 ``/runs/{id}/resume`` 重跑。

    典型场景：产出不符合工单协议导致运行挂起 —— 用户改好剧本/模型后点「重试」。
    """
    step = await session.get(CollabStep, step_id)
    if step is None:
        raise HTTPException(status_code=404, detail="找不到这一步")
    try:
        run = await store.get_run(session, step.run_id)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if step.ticket_id:
        ticket = await session.get(CollabTicket, step.ticket_id)
        if ticket is not None and ticket.state == "blocked":
            ticket.state = "open"
            ticket.updated_at = utcnow()
            session.add(ticket)
    run.status = "waiting_user"
    run.error = None
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    return await store.run_read(session, run)
