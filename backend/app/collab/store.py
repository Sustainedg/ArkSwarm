"""协作域的持久化（只依赖 tables / graph / schemas 与 SQLModel）。

这一层不做编排决策：它负责「把图存下来、把运行与工单读出来」，
所有判断都在 ``engine.py``（M1）与 ``graph.py`` 里。
"""
from __future__ import annotations

from typing import Any, Sequence

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import AgentProfile, new_id, utcnow
from ..settings import get_settings
from . import graph as graph_service
from .schemas import (
    BudgetSpec,
    ChatMessageRead,
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
from .tables import CollabMessage, CollabRun, CollabStep, CollabTeam, CollabTicket


class TeamNotFound(Exception):
    """协作组不存在。"""


class RunNotFound(Exception):
    """协作运行不存在。"""


class TeamInUse(Exception):
    """协作组下还有运行记录，不能删除（沿用既有 409 语义）。"""


class TeamKeyInUse(Exception):
    """要删改的节点 key 还被历史运行引用，改名会让这些运行找不到岗位（沿用既有 409 语义）。

    运行、工单与步骤里存的是**当时的 key**（``entry_key`` / ``holder_key`` / ``target_key``…），
    改名不会自动迁移：引擎下一步按 key 找节点，找不到就直接报「持有者/派发目标不在图里」。
    改名的意图与「删掉旧节点、新建一个」在图里长得一模一样，无从推断，所以只能在保存时拦下来。
    """


def to_read(team: CollabTeam, known_agent_profile_ids: set[str] | None = None) -> TeamRead:
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
        validation = graph_service.validate_graph(spec, team.entry_key, known_agent_profile_ids)
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


async def list_teams(
    session: AsyncSession, owner_id: str | None = None
) -> list[CollabTeam]:
    stmt = select(CollabTeam).order_by(CollabTeam.updated_at.desc())
    if owner_id is not None:
        stmt = stmt.where(CollabTeam.owner_id == owner_id)
    return list((await session.execute(stmt)).scalars().all())


async def get_team(session: AsyncSession, team_id: str) -> CollabTeam:
    team = await session.get(CollabTeam, team_id)
    if team is None:
        raise TeamNotFound(f"找不到协作组：{team_id}")
    return team


async def create_team(
    session: AsyncSession, payload: TeamCreate, owner_id: str | None = None
) -> CollabTeam:
    team = CollabTeam(
        name=payload.name.strip(),
        description=payload.description,
        entry_key=payload.entry_key,
        workspace_dir=payload.workspace_dir,
        graph=graph_service.to_storable(payload.graph),
        budget=graph_service.budget_to_storable(payload.budget),
        owner_id=owner_id,
    )
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team


async def _copy_team(source: CollabTeam, name: str, owner_id: str | None = None) -> CollabTeam:
    return CollabTeam(
        name=name,
        description=source.description,
        entry_key=source.entry_key,
        workspace_dir=source.workspace_dir,
        graph=dict(source.graph or {}),
        budget=dict(source.budget or {}),
        owner_id=owner_id,
    )


#: 每个 key 最多列几条引用（够用户定位是哪次运行/哪张工单即可，不必把看板全抄进错误里）
_MAX_KEY_REFERENCES = 3


def _removed_node_keys(old_graph: Any, new_graph: dict[str, Any]) -> set[str]:
    """这次保存「删掉/改掉」了哪些节点 key。

    改名与删除在图里是同一种变化（旧 key 消失），这里不做也无法做区分。
    旧图本身解析不了时不拦（让用户先把图存成能看的样子，别把两件事叠在一起报错）。
    """
    try:
        old = graph_service.from_storable(old_graph)
    except (ValueError, ValidationError):
        return set()
    old_keys = {node.key for node in old.nodes if node.key}
    new_keys = {str(node.get("key") or "") for node in (new_graph.get("nodes") or [])}
    return {key for key in old_keys - new_keys if key}


async def _stale_key_references(
    session: AsyncSession, team_id: str, removed: set[str]
) -> dict[str, list[str]]:
    """这些 key 还被这次协作组下的运行/工单/步骤/对话引用着多少处。"""
    run_ids = list(
        (await session.execute(select(CollabRun.id).where(CollabRun.team_id == team_id))).scalars()
    )
    if not run_ids:
        return {}
    refs: dict[str, list[str]] = {}

    def note(key: str | None, text: str) -> None:
        if key and key in removed and len(refs.get(key, [])) < _MAX_KEY_REFERENCES:
            refs.setdefault(key, []).append(text)

    for run_id, entry_key, title in (
        await session.execute(
            select(CollabRun.id, CollabRun.entry_key, CollabRun.title).where(
                CollabRun.team_id == team_id
            )
        )
    ).all():
        note(entry_key, f"运行「{title}」（{run_id[:8]}）的入口节点")
    for ticket_id, title, owner_key, holder_key, submitter_key, escalation in (
        await session.execute(
            select(
                CollabTicket.id,
                CollabTicket.title,
                CollabTicket.owner_key,
                CollabTicket.holder_key,
                CollabTicket.submitter_key,
                CollabTicket.escalation,
            ).where(CollabTicket.run_id.in_(run_ids))
        )
    ).all():
        note(owner_key, f"工单 {ticket_id[:8]}「{title}」的发起人")
        note(holder_key, f"工单 {ticket_id[:8]}「{title}」的承担者")
        note(submitter_key, f"工单 {ticket_id[:8]}「{title}」的提交人")
        escalation = escalation or {}
        note(str(escalation.get("from") or ""), f"工单 {ticket_id[:8]} 的上报人")
        for hop in escalation.get("hops") or []:
            note(str(hop), f"工单 {ticket_id[:8]} 的上报路径")
    for role_key, target_key in (
        await session.execute(
            select(CollabStep.role_key, CollabStep.target_key).where(
                CollabStep.run_id.in_(run_ids)
            )
        )
    ).all():
        note(role_key, "某一步骤的岗位")
        note(target_key, "某一步骤的派发目标")
    for role_key in (
        await session.execute(
            select(CollabMessage.role_key).where(CollabMessage.run_id.in_(run_ids))
        )
    ).all():
        note(role_key, "入口智能体对话的岗位")
    return refs


def _stale_key_message(team_name: str, refs: dict[str, list[str]]) -> str:
    """把「哪些 key 被谁引用着」写成一句能做决定的错误。"""
    detail = "；".join(
        f"「{key}」还被{'、'.join(items)}引用" for key, items in sorted(refs.items())
    )
    return (
        f"协作组「{team_name}」的节点 key 不能这样改：{detail}。"
        "运行与工单里记的是当时的 key，改掉之后它们会找不到对应岗位（直接报「持有者不在图里」）。"
        "请先删除这些运行记录，或把协作组复制一份在副本里改名。"
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
        new_graph = graph_service.to_storable(payload.graph)
        # 「存的草稿」不拦语义问题，但**删改已在运行的 key** 不是语义问题：它会打断运行，
        # 且事后无从修复（改名与删除不可区分），所以必须在保存这一层拦住。
        removed = _removed_node_keys(team.graph, new_graph)
        stale = await _stale_key_references(session, team_id, removed) if removed else {}
        if stale:
            raise TeamKeyInUse(_stale_key_message(team.name, stale))
        team.graph = new_graph
    if payload.budget is not None:
        team.budget = graph_service.budget_to_storable(payload.budget)
    team.updated_at = utcnow()
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team


async def duplicate_team(
    session: AsyncSession, team_id: str, owner_id: str | None = None
) -> CollabTeam:
    source = await get_team(session, team_id)
    copy = await _copy_team(source, f"{source.name}（副本）"[:120], owner_id=owner_id)
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
    """启动时把残留的 ``running`` 运行与 ``streaming`` 步骤标记为 ``interrupted``。

    进程重启后内存里的运行句柄一定已经失效，因此启动时看到 ``running`` 就是残留；
    工单与步骤原样保留，用户可以在界面上「继续」（铁律 9：绝不删库重建）。
    残留的 ``streaming`` 步骤同样要收尾：否则界面会一直显示「处理中」，点开却看不到任何输出。
    """
    rows: Sequence[CollabRun] = (
        await session.execute(select(CollabRun).where(CollabRun.status == RunStatus.RUNNING.value))
    ).scalars().all()
    for row in rows:
        row.status = RunStatus.INTERRUPTED.value
        row.error = row.error or "服务重启，这次协作被中断；可以点「继续」从当前工单接着跑"
        row.updated_at = utcnow()
        session.add(row)
    steps: Sequence[CollabStep] = (
        await session.execute(select(CollabStep).where(CollabStep.status == "streaming"))
    ).scalars().all()
    for step in steps:
        step.status = "interrupted"
        step.error = step.error or "服务重启，这一步被中断，没有留下输出"
        step.finished_at = utcnow()
        session.add(step)
    if rows or steps:
        await session.commit()
    await mark_chat_interrupted(
        session, None, "服务重启，这条 CEO 回复被中断；可以直接再问一次"
    )
    return len(rows)


# --- 运行 -------------------------------------------------------------------


def _ticket_read(ticket: CollabTicket) -> TicketRead:
    return TicketRead.model_validate(ticket, from_attributes=True)


def _chat_read(message: CollabMessage) -> ChatMessageRead:
    return ChatMessageRead.model_validate(message, from_attributes=True)


async def list_chat_messages(session: AsyncSession, run_id: str) -> list[ChatMessageRead]:
    """按发生顺序读出入口智能体对话。"""
    rows = (
        await session.execute(
            select(CollabMessage)
            .where(CollabMessage.run_id == run_id)
            .order_by(CollabMessage.sequence, CollabMessage.created_at, CollabMessage.id)
        )
    ).scalars().all()
    return [_chat_read(row) for row in rows]


async def create_run(
    session: AsyncSession, payload: RunCreate, owner_id: str | None = None
) -> CollabRun:
    team = await get_team(session, payload.team_id)
    run_id = new_id()
    # 优先级：本次任务显式指定 > 协作组默认 > 按运行 ID 自动分配
    workspace = (
        payload.workspace_dir
        or team.workspace_dir
        or str(get_settings().workspaces_root / f"collab-{run_id}")
    )
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
        owner_id=owner_id,
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


async def list_runs(
    session: AsyncSession,
    team_id: str | None = None,
    limit: int = 100,
    owner_id: str | None = None,
) -> list[CollabRun]:
    query = select(CollabRun).order_by(CollabRun.updated_at.desc()).limit(limit)
    if team_id:
        query = query.where(CollabRun.team_id == team_id)
    if owner_id is not None:
        query = query.where(CollabRun.owner_id == owner_id)
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
    """叫停或服务重启后，把这次运行连同残留步骤标记为中断（不删数据）。"""
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


async def mark_chat_interrupted(
    session: AsyncSession, run_id: str | None, reason: str
) -> int:
    """把残留的 ``streaming`` 聊天回复收尾，避免界面永远显示「CEO 正在回复」。"""
    query = select(CollabMessage).where(CollabMessage.status == "streaming")
    if run_id:
        query = query.where(CollabMessage.run_id == run_id)
    rows = (await session.execute(query)).scalars().all()
    for row in rows:
        row.status = "interrupted"
        row.error = row.error or reason
        row.finished_at = utcnow()
        session.add(row)
    if rows:
        await session.commit()
    return len(rows)


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
    """运行详情：工单、步骤与聊天记录。

    步骤只带**摘要**（``content`` / ``gist``）与 ``parts_count``，不带完整 ``parts``：
    一次长跑的 ``parts``（每次工具调用的入参出参、思考）能到几十 MB，全量返回会让
    「打开协作任务」慢到不可用。完整过程由 ``GET /runs/{id}/steps/{step_id}/parts``
    按需加载（见 :func:`step_parts`）。
    """
    team = await session.get(CollabTeam, run.team_id)
    tickets = (
        await session.execute(
            select(CollabTicket).where(CollabTicket.run_id == run.id).order_by(CollabTicket.created_at)
        )
    ).scalars().all()
    # 步骤摘要列先单独取；parts 仍然要再读一遍（省的是 HTTP 传输体，不是读库）：
    # parts_count / tool_names 必须从 parts 本体数出来，SQLite 里没有现成的统计列。
    step_columns = (
        CollabStep.id,
        CollabStep.run_id,
        CollabStep.ticket_id,
        CollabStep.agent_profile_id,
        CollabStep.role_key,
        CollabStep.duty,
        CollabStep.sequence,
        CollabStep.action,
        CollabStep.target_key,
        CollabStep.status,
        CollabStep.content,
        CollabStep.gist,
        CollabStep.usage,
        CollabStep.error,
        CollabStep.audit,
        CollabStep.started_at,
        CollabStep.finished_at,
    )
    steps = (
        await session.execute(
            select(*step_columns).where(CollabStep.run_id == run.id).order_by(CollabStep.sequence)
        )
    ).all()
    parts_rows = (
        await session.execute(
            select(CollabStep.id, CollabStep.parts).where(CollabStep.run_id == run.id)
        )
    ).all()
    parts_counts: dict[str, int] = {}
    tool_names: dict[str, list[str]] = {}
    for step_id, parts in parts_rows:
        parts_counts[step_id] = len(parts or [])
        names: list[str] = []
        for part in parts or []:
            name = part.get("name")
            if part.get("kind") == "tool_call" and name and name not in names:
                names.append(name)
                if len(names) >= 6:
                    break
        tool_names[step_id] = names
    profile_names = {
        profile.id: profile.name
        for profile in (await session.execute(select(AgentProfile))).scalars().all()
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
                agent_profile_id=step.agent_profile_id,
                profile_name="你" if step.role_key == "user" else profile_names.get(step.agent_profile_id or ""),
                role_key=step.role_key,
                duty=step.duty,
                sequence=step.sequence,
                action=step.action,
                target_key=step.target_key or "",
                status=step.status,
                content=step.content,
                gist=step.gist,
                parts=[],
                parts_count=parts_counts.get(step.id) or 0,
                tool_names=tool_names.get(step.id) or [],
                usage=step.usage or {},
                error=step.error,
                audit=step.audit or {},
                started_at=step.started_at,
                finished_at=step.finished_at,
            )
            for step in steps
        ],
        chat_messages=await list_chat_messages(session, run.id),
    )


async def step_parts(session: AsyncSession, run_id: str, step_id: str) -> list[dict[str, Any]]:
    """单个步骤的完整过程（懒加载）。步骤不存在或不属于这次运行时抛 :class:`RunNotFound`。"""
    row = (
        await session.execute(
            select(CollabStep.parts).where(CollabStep.id == step_id, CollabStep.run_id == run_id)
        )
    ).first()
    if row is None:
        raise RunNotFound(f"找不到协作步骤：{step_id}")
    return row[0] or []
