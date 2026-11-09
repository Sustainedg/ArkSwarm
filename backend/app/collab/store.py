"""协作域的持久化（只依赖 tables / graph / schemas 与 SQLModel）。

这一层不做编排决策：它负责「把图存下来、把运行与工单读出来」，
所有判断都在 ``engine.py``（M1）与 ``graph.py`` 里。
"""
from __future__ import annotations

from typing import Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import Role, new_id, utcnow
from ..settings import get_settings
from . import graph as graph_service
from .schemas import (
    BudgetSpec,
    GraphIssue,
    GraphSpec,
    GraphValidation,
    RunCreate,
    RunDetail,
    RunRead,
    RunStatus,
    StepRead,
    TeamCreate,
    TeamRead,
    TeamUpdate,
    TicketRead,
)
from .tables import CollabRun, CollabStep, CollabTeam, CollabTicket


class TeamNotFound(Exception):
    """协作组不存在。"""


class RunNotFound(Exception):
    """协作运行不存在。"""


class TeamInUse(Exception):
    """协作组下还有运行记录，不能删除（沿用既有 409 语义）。"""


def to_read(team: CollabTeam, known_role_ids: set[str] | None = None) -> TeamRead:
    """把行转成响应模型，并**每次都重新校验**图（编辑器要能实时看到问题）。"""
    try:
        spec = graph_service.from_storable(team.graph)
    except (ValueError, ValidationError):
        spec = GraphSpec()
        validation = GraphValidation(
            ok=False,
            errors=[
                GraphIssue(
                    code="graph.malformed",
                    message="编排图数据结构损坏，无法解析；请重新保存一次这张图",
                )
            ],
        )
    else:
        validation = graph_service.validate_graph(spec, team.entry_key, known_role_ids)
    try:
        budget = graph_service.budget_from_storable(team.budget)
    except (ValueError, ValidationError):
        budget = BudgetSpec()
    return TeamRead(
        id=team.id,
        name=team.name,
        description=team.description,
        entry_key=team.entry_key,
        workspace_dir=team.workspace_dir,
        graph=spec,
        budget=budget,
        node_count=len(spec.nodes),
        created_at=team.created_at,
        updated_at=team.updated_at,
        validation=validation,
    )


async def list_teams(session: AsyncSession) -> list[CollabTeam]:
    return list(
        (await session.execute(select(CollabTeam).order_by(CollabTeam.updated_at.desc()))).scalars().all()
    )


async def get_team(session: AsyncSession, team_id: str) -> CollabTeam:
    team = await session.get(CollabTeam, team_id)
    if team is None:
        raise TeamNotFound(f"找不到协作组：{team_id}")
    return team


async def create_team(session: AsyncSession, payload: TeamCreate) -> CollabTeam:
    team = CollabTeam(
        name=payload.name.strip(),
        description=payload.description,
        entry_key=payload.entry_key,
        workspace_dir=payload.workspace_dir,
        graph=graph_service.to_storable(payload.graph),
        budget=graph_service.budget_to_storable(payload.budget),
    )
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team


async def _copy_team(source: CollabTeam, name: str) -> CollabTeam:
    return CollabTeam(
        name=name,
        description=source.description,
        entry_key=source.entry_key,
        workspace_dir=source.workspace_dir,
        graph=dict(source.graph or {}),
        budget=dict(source.budget or {}),
    )


async def save_team(session: AsyncSession, team_id: str, payload: TeamUpdate) -> CollabTeam:
    """整图保存。语义错误不拦截（允许存草稿），但结构性错误已被 Pydantic 挡在路由层。"""
    team = await get_team(session, team_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("name"):
        team.name = str(data["name"]).strip()
    if "description" in data:
        team.description = data["description"]
    if "entry_key" in data:
        team.entry_key = data["entry_key"]
    if "workspace_dir" in data:
        team.workspace_dir = data["workspace_dir"]
    if payload.graph is not None:
        team.graph = graph_service.to_storable(payload.graph)
    if payload.budget is not None:
        team.budget = graph_service.budget_to_storable(payload.budget)
    team.updated_at = utcnow()
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team


async def duplicate_team(session: AsyncSession, team_id: str) -> CollabTeam:
    source = await get_team(session, team_id)
    copy = await _copy_team(source, f"{source.name}（副本）"[:120])
    session.add(copy)
    await session.commit()
    await session.refresh(copy)
    return copy


async def delete_team(session: AsyncSession, team_id: str) -> None:
    team = await get_team(session, team_id)
    runs = (await session.execute(select(CollabRun.id).where(CollabRun.team_id == team_id))).all()
    if runs:
        raise TeamInUse(f"协作组「{team.name}」下还有 {len(runs)} 次运行记录，请先删除这些运行")
    await session.delete(team)
    await session.commit()


async def count_runs(session: AsyncSession, team_id: str | None = None) -> int:
    query = select(CollabRun.id)
    if team_id:
        query = query.where(CollabRun.team_id == team_id)
    return len((await session.execute(query)).all())


async def recover_interrupted(session: AsyncSession) -> int:
    """启动时把残留的 ``running`` 运行标记为 ``interrupted``。

    进程重启后内存里的运行句柄一定已经失效，因此启动时看到 ``running`` 就是残留；
    工单与步骤原样保留，用户可以在界面上「继续」（铁律 9：绝不删库重建）。
    """
    rows: Sequence[CollabRun] = (
        await session.execute(select(CollabRun).where(CollabRun.status == RunStatus.RUNNING.value))
    ).scalars().all()
    for row in rows:
        row.status = RunStatus.INTERRUPTED.value
        row.error = row.error or "服务重启，这次协作被中断；可以点「继续」从当前工单接着跑"
        row.updated_at = utcnow()
        session.add(row)
    if rows:
        await session.commit()
    return len(rows)


# --- 运行 -------------------------------------------------------------------


def _ticket_read(ticket: CollabTicket) -> TicketRead:
    return TicketRead.model_validate(ticket, from_attributes=True)


async def create_run(session: AsyncSession, payload: RunCreate) -> CollabRun:
    team = await get_team(session, payload.team_id)
    run_id = new_id()
    workspace = team.workspace_dir or str(get_settings().workspaces_root / f"collab-{run_id}")
    run = CollabRun(
        id=run_id,
        team_id=team.id,
        title=(payload.title or "新协作").strip() or "新协作",
        goal="",
        # 建好之后先等用户开口（第一条消息就是这次协作的目标）
        status=RunStatus.WAITING_USER.value,
        entry_key=team.entry_key,
        workspace_dir=workspace,
        budget=dict(team.budget or {}),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def get_run(session: AsyncSession, run_id: str) -> CollabRun:
    run = await session.get(CollabRun, run_id)
    if run is None:
        raise RunNotFound(f"找不到协作运行：{run_id}")
    return run


async def list_runs(session: AsyncSession, team_id: str | None = None, limit: int = 100) -> list[CollabRun]:
    query = select(CollabRun).order_by(CollabRun.updated_at.desc()).limit(limit)
    if team_id:
        query = query.where(CollabRun.team_id == team_id)
    return list((await session.execute(query)).scalars().all())


async def delete_run(session: AsyncSession, run_id: str) -> None:
    run = await get_run(session, run_id)
    tickets = (
        await session.execute(select(CollabTicket).where(CollabTicket.run_id == run_id))
    ).scalars().all()
    # 先解开父子引用，再删工单（否则父工单被删时子工单会短暂悬空）
    for ticket in tickets:
        ticket.parent_ticket_id = None
    await session.commit()
    for ticket in tickets:
        steps = (
            await session.execute(select(CollabStep).where(CollabStep.ticket_id == ticket.id))
        ).scalars().all()
        for step in steps:
            step.ticket_id = None
        await session.delete(ticket)
    await session.commit()
    await session.delete(run)
    await session.commit()


async def mark_interrupted(session: AsyncSession, run_id: str, reason: str) -> None:
    """客户端断开或服务重启后，把这次运行标记为中断（不删数据）。"""
    run = await session.get(CollabRun, run_id)
    if run is None:
        return
    if run.status == RunStatus.RUNNING.value:
        run.status = RunStatus.INTERRUPTED.value
    run.error = run.error or reason
    run.updated_at = utcnow()
    session.add(run)
    steps = (
        await session.execute(
            select(CollabStep).where(CollabStep.run_id == run_id, CollabStep.status == "streaming")
        )
    ).scalars().all()
    for step in steps:
        step.status = "interrupted"
        step.error = step.error or reason
        step.finished_at = utcnow()
        session.add(step)
    await session.commit()


async def run_read(session: AsyncSession, run: CollabRun) -> RunRead:
    """运行列表项：只算计数，不把工单与步骤全捞出来。"""
    team = await session.get(CollabTeam, run.team_id)
    states = [
        row[0]
        for row in (
            await session.execute(select(CollabTicket.state).where(CollabTicket.run_id == run.id))
        ).all()
    ]
    return RunRead(
        id=run.id,
        team_id=run.team_id,
        team_name=team.name if team else None,
        title=run.title,
        goal=run.goal,
        budget=graph_service.budget_from_storable(run.budget),
        status=run.status,
        entry_key=run.entry_key,
        workspace_dir=run.workspace_dir,
        steps_used=run.steps_used,
        tickets_used=run.tickets_used,
        usage=run.usage or {},
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
        ticket_count=len(states),
        open_tickets=sum(
            1
            for state in states
            if state in ("open", "working", "review", "rework", "waiting_children", "blocked")
        ),
    )


async def run_detail(session: AsyncSession, run: CollabRun) -> RunDetail:
    team = await session.get(CollabTeam, run.team_id)
    tickets = (
        await session.execute(
            select(CollabTicket).where(CollabTicket.run_id == run.id).order_by(CollabTicket.created_at)
        )
    ).scalars().all()
    steps = (
        await session.execute(
            select(CollabStep).where(CollabStep.run_id == run.id).order_by(CollabStep.sequence)
        )
    ).scalars().all()
    role_names = {
        role.id: role.name
        for role in (await session.execute(select(Role))).scalars().all()
    }
    read = await run_read(session, run)
    return RunDetail(
        **read.model_dump(),
        tickets=[_ticket_read(t) for t in tickets],
        steps=[
            StepRead(
                id=step.id,
                run_id=step.run_id,
                ticket_id=step.ticket_id,
                role_id=step.role_id,
                agent_id=step.agent_id,
                role_name="你" if step.role_key == "user" else role_names.get(step.role_id or ""),
                role_key=step.role_key,
                duty=step.duty,
                sequence=step.sequence,
                action=step.action,
                status=step.status,
                content=step.content,
                gist=step.gist,
                parts=step.parts or [],
                usage=step.usage or {},
                error=step.error,
                audit=step.audit or {},
                started_at=step.started_at,
                finished_at=step.finished_at,
            )
            for step in steps
        ],
    )
