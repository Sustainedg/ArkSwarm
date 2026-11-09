"""协作域的 HTTP 接口（薄层：只做参数校验、状态码与序列化）。

编排逻辑在 ``engine.py``（M1 落地），图的判断在 ``graph.py``。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import SessionLocal, get_session
from ..models import Role, utcnow
from ..transport.sse import sse_stream
from . import engine, execution, graph as graph_service, store
from .schemas import (
    GraphSpec,
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


def _conflict(exc: store.TeamInUse) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


async def _known_role_ids(session: AsyncSession) -> set[str]:
    """把「角色是否还存在」这类事实注入纯函数，校验层不碰数据库。"""
    rows = (await session.execute(select(Role.id))).all()
    return {row[0] for row in rows}


@router.get("/teams")
async def list_teams(session: AsyncSession = Depends(get_session)) -> dict:
    rows = await store.list_teams(session)
    known = await _known_role_ids(session)
    return {"items": [store.to_read(row, known) for row in rows]}


@router.post("/teams", status_code=201)
async def create_team(payload: TeamCreate, session: AsyncSession = Depends(get_session)) -> TeamRead:
    team = await store.create_team(session, payload)
    return store.to_read(team, await _known_role_ids(session))


@router.get("/teams/{team_id}")
async def get_team(team_id: str, session: AsyncSession = Depends(get_session)) -> TeamRead:
    try:
        team = await store.get_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_role_ids(session))


@router.put("/teams/{team_id}")
async def save_team(
    team_id: str, payload: TeamUpdate, session: AsyncSession = Depends(get_session)
) -> TeamRead:
    try:
        team = await store.save_team(session, team_id, payload)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_role_ids(session))


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
    known = await _known_role_ids(session)
    return graph_service.validate_graph(spec, entry_key, known).model_dump()


@router.post("/teams/{team_id}/duplicate", status_code=201)
async def duplicate_team(team_id: str, session: AsyncSession = Depends(get_session)) -> TeamRead:
    try:
        team = await store.duplicate_team(session, team_id)
    except store.TeamNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return store.to_read(team, await _known_role_ids(session))


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
    return await store.run_detail(session, run)


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    execution.cancel_run(run_id)
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
    cancelled = execution.cancel_run(run_id)
    if not cancelled and run.status == "running":
        await store.mark_interrupted(session, run_id, "已停止")
    return {"ok": True, "cancelled": cancelled}


def _stream_turn(prepared: engine.PreparedTurn) -> StreamingResponse:
    """把一次推进包装成 SSE；客户端断开时同步取消子进程并标记中断。"""
    run_id = prepared.run.id

    async def event_source() -> AsyncIterator[dict]:
        async with SessionLocal() as session:
            run = await session.get(CollabRun, run_id)
            if run is not None:
                prepared.run = run
            async for event in engine.consume_turn(session, prepared):
                yield event.to_dict()

    async def guarded() -> AsyncIterator[str]:
        try:
            async for chunk in sse_stream(event_source()):
                yield chunk
        except asyncio.CancelledError:
            execution.cancel_run(run_id)
            async with SessionLocal() as session:
                await store.mark_interrupted(session, run_id, "客户端断开连接，这次协作被中断")
            raise

    return StreamingResponse(
        guarded(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/runs/{run_id}/messages")
async def send_message(
    run_id: str, payload: RunMessage, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    try:
        prepared = await engine.prepare_turn(session, run_id, payload.content)
    except store.RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except engine.TurnError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _stream_turn(prepared)


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """从中断处继续（不追加用户消息）。"""
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
