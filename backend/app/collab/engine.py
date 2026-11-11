"""编排状态机：把「一张图 + 一张工单」变成一条**串行推进**的流水线。

一次 :func:`consume_turn` 会一直推进到「需要用户介入」或「全部完成」为止：

```
用户消息 → 入口节点 → 工单被派发/拆解 → 实施者提交 → 审查者判定
                                             ↑                    │
                                             └──── rework 回边 ────┘
```

设计要点（对应 docs/collaboration-design.md §3 / §5）：

* **串行**：同一时刻只有一个步骤在跑，因此"谁在等谁"永远确定。
* **工单是唯一的交接物**：每个步骤的输入由 ``prompt.py`` 从工单字段拼出来，
  不携带任何角色的对话原文。
* **每一步都落库**：``collab_steps`` 既用于展示时间线，也用于审计与重跑。
* **预算与轮次上限**：任何一项超限都挂起（``waiting_user``）并给出可读原因，绝不静默。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Sequence

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..models import Role, utcnow
from ..agents.events import AgentEvent, EventType
from . import execution, protocol
from .graph import (
    allowed_assignees,
    budget_from_storable,
    effective_max_rounds,
    effective_output_contract,
    entry_node,
    from_storable,
    node_map,
    reviewer_of,
    rework_edge,
    rework_targets,
    validate_graph,
)
from .prompt import HistoryEntry, TicketView, build_step_prompt, ticket_code
from .tickets import (
    ensure_ask_options,
    extract_body,
    parse_work_order,
    resolve_order_targets,
    validate_order,
)
from .schemas import (
    DeliveryMode,
    GraphSpec,
    NodeSpec,
    RunStatus,
    TicketAction,
    TicketState,
    VERDICT_CHANGES,
    VERDICT_PASS,
)
from .tables import CollabRun, CollabStep, CollabTeam, CollabTicket

#: 防御性上限：即使预算配错了，也不允许一个请求里无限循环
MAX_STEPS_PER_TURN = 200
#: 拆解树的最大层数：防止「拆子工单再拆子工单」无穷套娃
MAX_TICKET_DEPTH = 5
#: 同一个父工单下、同名子工单最多拆几次：超过就挂起让人判断，避免"复读式拆解"空转烧钱
MAX_DUPLICATE_CHILD_TITLES = 2
FINISHED_STATES = {TicketState.DONE.value, TicketState.REJECTED.value}
#: 「该有人动手」的状态：串行推进时引擎就绪队列取的就是这些
READY_STATES = {
    TicketState.OPEN.value,
    TicketState.WORKING.value,
    TicketState.REVIEW.value,
    TicketState.REWORK.value,
}
ACTIVE_STATES = {
    TicketState.OPEN.value,
    TicketState.WORKING.value,
    TicketState.WAITING_CHILDREN.value,
    TicketState.REVIEW.value,
    TicketState.REWORK.value,
    TicketState.BLOCKED.value,
}


class TurnError(Exception):
    """这次请求没法开始（配置缺失、图不合法、已经在跑等）。"""


@dataclass
class PreparedTurn:
    run: CollabRun
    team: CollabTeam
    graph: GraphSpec
    entry: NodeSpec
    content: str | None = None


@dataclass
class TurnState:
    """异步生成器没法返回值，用这个可变载体把控制信号带回来。"""

    stop: bool = False
    reason: str | None = None
    status: str | None = None
    budget_exhausted: bool = False


# --- 准备 -------------------------------------------------------------------


async def _tickets(session: AsyncSession, run_id: str) -> list[CollabTicket]:
    return list(
        (
            await session.execute(
                select(CollabTicket)
                .where(CollabTicket.run_id == run_id)
                .order_by(CollabTicket.created_at, CollabTicket.id)
            )
        )
        .scalars()
        .all()
    )


async def _next_sequence(session: AsyncSession, run_id: str) -> int:
    rows = (
        await session.execute(select(CollabStep.sequence).where(CollabStep.run_id == run_id))
    ).all()
    return max((row[0] for row in rows), default=0) + 1


def _first_line(text: str, limit: int = 60) -> str:
    line = (text or "").strip().splitlines()[0] if text.strip() else "新协作"
    return line[:limit]


async def prepare_turn(session: AsyncSession, run_id: str, content: str | None) -> PreparedTurn:
    """校验图、把用户的这句话接到入口节点的工单上。"""
    run = await session.get(CollabRun, run_id)
    if run is None:
        raise TurnError(f"找不到协作运行：{run_id}")
    if execution.is_running(run_id):
        raise TurnError("这次协作已经在运行中，请先等它结束或点「停止」")

    team = await session.get(CollabTeam, run.team_id)
    if team is None:
        raise TurnError("协作组已被删除")
    try:
        graph = from_storable(team.graph)
    except ValueError as exc:
        raise TurnError(f"编排图无法解析：{exc}") from None

    validation = validate_graph(graph, team.entry_key)
    if not validation.ok:
        detail = "；".join(issue.message for issue in validation.errors[:3])
        raise TurnError(f"编排图还不能跑：{detail}")

    entry = entry_node(graph, team.entry_key)
    assert entry is not None  # validate_graph 已经保证

    text = (content or "").strip()
    tickets = await _tickets(session, run_id)
    root = next((t for t in tickets if t.parent_ticket_id is None), None)

    if text:
        if run.goal == "":
            run.goal = text
            if run.title in ("", "新协作"):
                run.title = _first_line(text)
        # 用户说的话也要出现在时间线上
        session.add(
            CollabStep(
                run_id=run.id,
                ticket_id=root.id if root else None,
                role_key="user",
                duty="user",
                sequence=await _next_sequence(session, run.id),
                action="",
                status="complete",
                content=text,
                finished_at=utcnow(),
            )
        )

    if root is None:
        if not text:
            raise TurnError("请先告诉入口角色你想做什么")
        root = CollabTicket(
            run_id=run.id,
            parent_ticket_id=None,
            from_role_id=None,
            to_role_id=entry.role_id,
            owner_key=entry.key,
            holder_key=entry.key,
            title=_first_line(text),
            brief={"goal": text},
            state=TicketState.OPEN.value,
            max_rounds=effective_max_rounds(entry, budget_from_storable(run.budget)),
        )
        session.add(root)
        run.tickets_used = (run.tickets_used or 0) + 1
        await session.commit()
        await session.refresh(root)
    elif text and root.state == TicketState.BLOCKED.value:
        # 入口角色问过话，用户现在回答了 → 工单重新回到提问者（当前持有者）手里
        root.state = TicketState.OPEN.value
        # 上一轮的提问已经答复，清掉卡片数据；否则下一次因别的原因挂起时会把旧卡片带出来
        result = dict(root.result or {})
        result.pop("question", None)
        result.pop("options", None)
        root.result = result
    elif text and root.state == TicketState.DONE.value:
        # 运行完成后用户仍可继续找入口角色：重新打开根工单，让领导者接手这次追问。
        # 历史工单与产出保留，后续派活会继续挂在同一棵工单树上。
        root.state = TicketState.OPEN.value
        root.holder_key = entry.key
        root.to_role_id = entry.role_id
        root.closed_at = None
        root.verdict = None

    run.status = RunStatus.RUNNING.value
    run.error = None
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return PreparedTurn(run=run, team=team, graph=graph, entry=entry, content=text or None)


# --- 推进 -------------------------------------------------------------------


async def consume_turn(session: AsyncSession, prepared: PreparedTurn) -> AsyncIterator[protocol.CollabEvent]:
    """推进这次协作，直到需要用户介入或全部完成。"""
    run_id = prepared.run.id
    state = TurnState()
    steps_this_turn = 0
    first_step = True

    yield protocol.run_started(run_id, prepared.run.team_id, prepared.run.title, prepared.run.entry_key)

    while not state.stop:
        run = await session.get(CollabRun, run_id)
        if run is None:
            yield protocol.run_status(RunStatus.ERROR.value, "协作运行已被删除")
            return

        async for event in _rollup_parents(session, run):
            yield event

        reason = _budget_reason(run, prepared)
        if reason:
            state.stop, state.reason, state.budget_exhausted = True, reason, True
            break

        ready = [t for t in await _tickets(session, run_id) if t.state in READY_STATES]
        if not ready:
            break

        ticket = ready[0]
        node = node_map(prepared.graph).get(ticket.holder_key)
        if node is None:
            state.stop = True
            state.reason = f"工单 {ticket_code(ticket.id)} 的持有者「{ticket.holder_key}」不在图里"
            state.status = RunStatus.ERROR.value
            break

        async for event in _run_step(
            session,
            prepared,
            run,
            ticket,
            node,
            state,
            user_message=prepared.content if first_step else None,
        ):
            yield event
        first_step = False
        steps_this_turn += 1
        if steps_this_turn >= MAX_STEPS_PER_TURN:
            state.stop = True
            state.reason = "单次请求内推进的步骤过多，已暂停（请稍后点「继续」）"
            break

    async for event in _finalize(session, prepared, state):
        yield event


def _elapsed_seconds(started: datetime | None) -> float:
    """SQLite 取回的 datetime 不带时区，这里统一按 UTC 解释。"""
    if started is None:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (utcnow() - started).total_seconds()


def _budget_reason(run: CollabRun, prepared: PreparedTurn) -> str | None:
    budget = budget_from_storable(run.budget)
    if (run.steps_used or 0) >= budget.max_steps:
        return f"已达步数上限（{budget.max_steps} 步）"
    if (run.tickets_used or 0) >= budget.max_tickets:
        return f"已达工单上限（{budget.max_tickets} 张）"
    if budget.max_tokens:
        used = sum(v for v in (run.usage or {}).values() if isinstance(v, (int, float)))
        if used >= budget.max_tokens:
            return f"已达 token 上限（{budget.max_tokens}）"
    # 交互式协作的时限按「多久没有进展」计算，而不是从创建时间起累计：
    # 用户隔一段时间回来继续、或运行在 waiting_user 停留很久，都不该让它永久失效。
    # prepare_turn 会刷新 updated_at；每一步成功落库也会刷新它。
    elapsed = _elapsed_seconds(run.updated_at)
    if elapsed > budget.timeout_seconds:
        return f"距上次进展已超过运行时限（{budget.timeout_seconds} 秒）"
    return None


# --- 单步 -------------------------------------------------------------------


async def _run_step(
    session: AsyncSession,
    prepared: PreparedTurn,
    run: CollabRun,
    ticket: CollabTicket,
    node: NodeSpec,
    state: TurnState,
    *,
    user_message: str | None,
) -> AsyncIterator[protocol.CollabEvent]:
    graph = prepared.graph
    assignees = allowed_assignees(graph, node.key)
    reviewer = reviewer_of(graph, node.key)
    rework = rework_targets(graph, node.key)
    labels = {key: (spec.label or key) for key, spec in node_map(graph).items()}
    label_counts: dict[str, int] = {}
    for label in labels.values():
        if label:
            label_counts[label] = label_counts.get(label, 0) + 1
    # 只接受唯一的显示名映射；同名节点必须由模型写 key，避免猜错接收人
    aliases = {
        label: key
        for key, label in labels.items()
        if label and label != key and label_counts[label] == 1
    }

    history = await _ticket_history(session, ticket.id)
    prompt_text = build_step_prompt(
        team_name=prepared.team.name,
        workspace_path=str(execution.workspace_root(run)),
        node=node,
        ticket=TicketView(
            id=ticket.id,
            title=ticket.title,
            brief=ticket.brief or {},
            state=ticket.state,
            round=ticket.round,
            result=ticket.result or {},
            review_comment=ticket.review_comment,
            submitter_key=ticket.submitter_key or "",
        ),
        history=history,
        assignees=assignees,
        reviewer=reviewer,
        rework_targets=rework,
        user_message=user_message,
        node_labels=labels,
    )

    step = CollabStep(
        run_id=run.id,
        ticket_id=ticket.id,
        role_key=node.key,
        duty=node.duty.value,
        sequence=await _next_sequence(session, run.id),
        status="streaming",
    )
    session.add(step)
    # 只把「还没人碰过」的工单标成处理中；review / rework 的状态要原样带进 prompt
    if ticket.state == TicketState.OPEN.value:
        ticket.state = TicketState.WORKING.value
    session.add(ticket)
    await session.commit()
    await session.refresh(step)
    await session.refresh(ticket)

    try:
        prepared_step = await execution.prepare_step(
            session, run=run, node=node, step_id=step.id, prompt=prompt_text
        )
    except execution.StepError as exc:
        step.status, step.error, step.finished_at = "error", str(exc), utcnow()
        step.role_id, step.agent_id = node.role_id, None
        session.add(step)
        await session.commit()
        yield protocol.step_finished(step.id, "error", error=str(exc))
        state.stop, state.reason, state.status = True, str(exc), RunStatus.ERROR.value
        return

    step.role_id = prepared_step.role.id
    step.agent_id = prepared_step.agent.id
    session.add(step)
    await session.commit()

    yield protocol.step_started(
        step.id, run.id, ticket.id, node.key, node.duty.value, prepared_step.role.name, prepared_step.agent.name
    )
    for warning in prepared_step.warnings:
        yield protocol.notice(warning, "warn", step_id=step.id)

    handle = execution.registry.register(run.id, step.id, prepared_step.agent.name)
    text_parts: list[str] = []
    parts: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    error_text: str | None = None
    step_status = "complete"

    try:
        async for event in execution.stream_step(prepared_step, handle):
            _accumulate(event, text_parts, parts, usage)
            if event.type is EventType.ERROR:
                error_text = event.text or "智能体返回了错误"
            elif event.type is EventType.RUN_FINISHED:
                status = (event.payload or {}).get("status")
                if status in ("error", "cancelled"):
                    step_status = status
            yield protocol.step_event(step.id, event)
    except Exception as exc:  # noqa: BLE001 - 执行失败也要落库并显式上报（铁律 11）
        error_text = str(exc)
        step_status = "error"
        yield protocol.step_event(step.id, AgentEvent(EventType.ERROR, text=str(exc)))
    finally:
        execution.registry.finish(step.id)

    audit = execution.finish_audit(prepared_step)
    raw_text = "".join(text_parts)
    body = ""
    order = None
    parse_warning: str | None = None

    if step_status == "complete" and error_text is None:
        parsed = parse_work_order(raw_text)
        if not parsed.ok or parsed.order is None:
            error_text = f"产出不符合工单协议：{parsed.error or '未给出 work_order 块'}"
            step_status = "error"
        else:
            body = extract_body(raw_text)
            parse_warning = parsed.warning
            resolve_order_targets(parsed.order, aliases)
            # ask_user 漏给 options 时不把用户堵在协议错误上：补成可点卡片继续推进。
            ensure_ask_options(parsed.order)
            semantic = validate_order_ctx(
                parsed.order, node, assignees, rework, is_entry=node.key == prepared.entry.key
            )
            if semantic:
                error_text = semantic
                step_status = "error"
            else:
                order = parsed.order

    step.content = body or raw_text
    step.parts = parts
    step.usage = usage
    step.error = error_text
    step.status = step_status
    step.audit = audit
    step.action = order.action.value if order is not None else ""
    # 记下这一步把活交给了谁：岗位会话据此还原「谁对谁说了什么」的交接链
    step.target_key = (order.assignee or "") if order is not None and order.action is TicketAction.DELEGATE else ""
    step.gist = _order_gist(order) if order is not None else (error_text or "")[:160]
    step.finished_at = utcnow()

    session.add(step)
    run.steps_used = (run.steps_used or 0) + 1
    run.usage = _merge_usage(run, usage)
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()

    if parse_warning:
        yield protocol.notice(parse_warning, "warn", step_id=step.id)

    if audit and not audit.get("unchanged", True):
        yield protocol.notice(
            f"越权提醒：「{prepared_step.role.name}」本该只读，但共享工作目录有 "
            f"{audit.get('count')} 个文件发生变化（审计已记录）",
            "warn",
            step_id=step.id,
        )

    yield protocol.step_finished(
        step.id,
        step_status,
        action=step.action,
        usage=usage,
        error=error_text,
    )

    if step_status == "cancelled":
        state.stop, state.status, state.reason = True, RunStatus.CANCELLED.value, "用户停止了这次协作"
        return

    if step_status != "complete" or error_text:
        # 解析/执行失败 → 挂起等用户，绝不猜也不静默
        ticket.state = TicketState.BLOCKED.value
        session.add(ticket)
        run.error = error_text
        await session.commit()
        state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, error_text
        return

    async for event in _safe_apply(session, prepared, run, ticket, node, order, reviewer, state):
        yield event


def validate_order_ctx(
    order, node: NodeSpec, assignees: Sequence[str], rework: Sequence[str], *, is_entry: bool
) -> str | None:
    """把「节点允许的动作/对象」喂给纯函数做语义校验。"""
    return validate_order(
        order,
        allowed_actions=effective_output_contract(node),
        allowed_assignees=assignees,
        allowed_rework_targets=rework,
        is_entry=is_entry,
    )


def _order_gist(order: Any, limit: int = 160) -> str:
    """把一次动作压成一句话摘要，**只取结构化字段**，绝不引用原始正文。

    这是「只传工单、不传对话」的结构性保证：下游能看到的摘要全部由引擎派生。
    """
    text = ""
    if order.action is TicketAction.DELEGATE:
        target = order.assignee or "?"
        text = f"{order.mode.value} → {target}"
        if order.ticket is not None and order.ticket.title:
            text += f"：{order.ticket.title}"
    elif order.action is TicketAction.SUBMIT:
        text = order.summary or "已提交"
    elif order.action is TicketAction.REVIEW:
        text = order.verdict or ""
        if order.review_comment:
            text += f"：{order.review_comment}"
    elif order.action is TicketAction.ASK_USER:
        text = order.question or "向用户提问"
    elif order.action is TicketAction.REJECT:
        text = order.reason or order.review_comment or "否决"
    else:
        text = order.remark or "确认完成"
    text = " ".join(text.split())
    return text[:limit]


def _accumulate(event: AgentEvent, texts: list[str], parts: list[dict[str, Any]], usage: dict[str, Any]) -> None:
    """与 ``runtime/service._merge_part`` 同构：把增量按真实发生顺序并进时间线。"""
    if event.type is EventType.MESSAGE_DELTA and event.text:
        texts.append(event.text)
        _merge_part(parts, "text", event.text)
    elif event.type is EventType.THINKING_DELTA and event.text:
        _merge_part(parts, "thinking", event.text)
    elif event.type is EventType.TOOL_CALL:
        parts.append({"kind": "tool_call", "id": event.call_id, "name": event.name, **(event.payload or {})})
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
        usage.update({k: v for k, v in (event.payload or {}).items() if v is not None})


def _merge_part(parts: list[dict[str, Any]], kind: str, text: str) -> None:
    if parts and parts[-1].get("kind") == kind:
        parts[-1]["text"] = (parts[-1].get("text") or "") + text
    else:
        parts.append({"kind": kind, "text": text})


def _merge_usage(run: CollabRun, usage: dict[str, Any]) -> dict[str, Any]:
    merged = dict(run.usage or {})
    for key, value in usage.items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = merged[key] + value
        else:
            merged[key] = value
    return merged


async def _ticket_history(session: AsyncSession, ticket_id: str) -> list[HistoryEntry]:
    """只取**这一张工单**的历史摘要。

    摘要来自 ``step.gist``（引擎从工单字段派生），**不取 ``content``** ——
    这样"下游看不到上游对话原文"就不是靠自觉，而是结构上没有那条数据通路。
    """
    rows = (
        await session.execute(
            select(CollabStep)
            .where(CollabStep.ticket_id == ticket_id, CollabStep.role_key != "user")
            .order_by(CollabStep.sequence)
        )
    ).scalars().all()
    entries: list[HistoryEntry] = []
    for row in rows:
        gist = row.gist or row.error or ""
        entries.append(
            HistoryEntry(role_label=row.role_key, action=row.action or row.status, gist=gist or "（无摘要）")
        )
    return entries


# --- 动作 -------------------------------------------------------------------


async def _ticket_depth(session: AsyncSession, ticket: CollabTicket) -> int:
    """工单在拆解树里的层数（根工单是 0）；只向上走 parent 链。"""
    depth = 0
    current = ticket
    while current.parent_ticket_id and depth <= MAX_TICKET_DEPTH + 1:
        parent = await session.get(CollabTicket, current.parent_ticket_id)
        if parent is None:
            break
        current = parent
        depth += 1
    return depth


async def _child_count_with_title(session: AsyncSession, parent_id: str, title: str) -> int:
    """同一父工单下、同名子工单已经拆过几次（含已完成/被否决的，用来发现"复读式拆解"）。"""
    rows = (
        await session.execute(
            select(CollabTicket.id).where(
                CollabTicket.parent_ticket_id == parent_id, CollabTicket.title == title
            )
        )
    ).all()
    return len(rows)


async def _reject_action(
    session: AsyncSession, run: CollabRun, ticket: CollabTicket, state: TurnState, message: str
) -> None:
    """显式拒绝一次动作：工单挂起、运行置 ``waiting_user``，绝不静默（铁律 11）。"""
    ticket.state = TicketState.BLOCKED.value
    ticket.updated_at = utcnow()
    session.add(ticket)
    run.error = message
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()
    await session.refresh(ticket)
    state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, message


async def _apply_action(
    session: AsyncSession,
    prepared: PreparedTurn,
    run: CollabRun,
    ticket: CollabTicket,
    node: NodeSpec,
    order,
    reviewer: str | None,
    state: TurnState,
) -> AsyncIterator[protocol.CollabEvent]:
    graph = prepared.graph
    nodes = node_map(graph)
    action = order.action

    if action is TicketAction.DELEGATE:
        target = nodes.get(order.assignee or "")
        if target is None:
            yield protocol.notice(f"派发目标「{order.assignee}」不在图里", "error")
            return
        if order.mode is DeliveryMode.SPLIT:
            draft = order.ticket
            title = (draft.title if draft else "") or "子任务"
            depth = await _ticket_depth(session, ticket)
            if depth >= MAX_TICKET_DEPTH:
                message = (
                    f"工单 {ticket_code(ticket.id)} 已经拆到第 {depth} 层（上限 {MAX_TICKET_DEPTH} 层），"
                    "再拆下去只会无穷套娃：请改用 handoff 直接交给实施者，或调整这次协作的拆解方式"
                )
                # 提示统一在本次推进收尾时发出（_finalize 会带上 state.reason），这里不重复
                await _reject_action(session, run, ticket, state, message)
                yield protocol.ticket_updated(_ticket_payload(ticket))
                return
            repeats = await _child_count_with_title(session, ticket.id, title)
            if repeats >= MAX_DUPLICATE_CHILD_TITLES:
                message = (
                    f"同一个父工单下已经拆出过 {repeats} 张同名子工单「{title}」，"
                    "再拆同样的活只会空转：请换一个更具体的标题，或者由你确认这件事到底该怎么做"
                )
                await _reject_action(session, run, ticket, state, message)
                yield protocol.ticket_updated(_ticket_payload(ticket))
                return
            child = CollabTicket(
                run_id=run.id,
                parent_ticket_id=ticket.id,
                from_role_id=node.role_id,
                to_role_id=target.role_id,
                owner_key=node.key,
                holder_key=target.key,
                title=title,
                brief=(draft.to_dict() if draft else {}),
                state=TicketState.OPEN.value,
                max_rounds=effective_max_rounds(target, budget_from_storable(run.budget)),
            )
            # 谁拆的谁汇总：子工单全部完成后，父工单回到这个节点手里
            ticket.owner_key = node.key
            ticket.state = TicketState.WAITING_CHILDREN.value
            session.add(child)
            session.add(ticket)
            run.tickets_used = (run.tickets_used or 0) + 1
            session.add(run)
            await session.commit()
            await session.refresh(child)
            await session.refresh(ticket)
            yield protocol.ticket_created(_ticket_payload(child))
            yield protocol.ticket_updated(_ticket_payload(ticket))
            return

        # handoff：同一张工单换人做
        ticket.owner_key = ticket.owner_key or node.key
        ticket.holder_key = target.key
        ticket.to_role_id = target.role_id
        ticket.state = TicketState.WORKING.value
        if order.ticket and order.ticket.title:
            ticket.title = order.ticket.title
            ticket.brief = {**(ticket.brief or {}), **order.ticket.to_dict()}
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.SUBMIT:
        ticket.result = {
            "summary": order.summary or "",
            "artifacts": order.artifacts,
        }
        ticket.submitter_key = node.key
        if reviewer:
            target = nodes.get(reviewer)
            ticket.state = TicketState.REVIEW.value
            ticket.holder_key = reviewer
            if target is not None:
                ticket.to_role_id = target.role_id
        else:
            ticket.state = TicketState.DONE.value
            ticket.closed_at = utcnow()
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.REVIEW:
        if order.verdict == VERDICT_PASS:
            ticket.state = TicketState.DONE.value
            ticket.verdict = VERDICT_PASS
            ticket.closed_at = utcnow()
            ticket.updated_at = utcnow()
            session.add(ticket)
            await session.commit()
            await session.refresh(ticket)
            yield protocol.ticket_updated(_ticket_payload(ticket))
            return

        targets = rework_targets(graph, node.key)
        destination = order.rework_to or (
            ticket.submitter_key if ticket.submitter_key in targets else ""
        )
        if not destination:
            destination = targets[0] if len(targets) == 1 else ""
        target_node = nodes.get(destination)
        if target_node is None:
            message = (
                f"打回目标不明确：审查者「{node.key}」有多条退路"
                f"（{'、'.join(targets) or '无'}），需要在工单里用 rework_to 指明"
            )
            ticket.state = TicketState.BLOCKED.value
            run.error = message
            session.add(ticket)
            session.add(run)
            await session.commit()
            await session.refresh(ticket)
            yield protocol.notice(message, "error")
            yield protocol.ticket_updated(_ticket_payload(ticket))
            state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, message
            return
        edge = rework_edge(graph, node.key, destination)
        cap = ticket.max_rounds or 0
        if edge is not None and edge.max_rounds:
            cap = min(cap, edge.max_rounds) if cap else edge.max_rounds
        if cap != (ticket.max_rounds or 0):
            # 边级上限更紧时，落到工单上（界面显示的「返工 x/y」就是它）
            ticket.max_rounds = cap
        ticket.round = (ticket.round or 0) + 1
        if cap and ticket.round > cap:
            message = (
                f"工单 {ticket_code(ticket.id)} 已返工 {ticket.round - 1} 轮仍未通过（上限 {cap} 轮），"
                "需要你介入：要么放宽这条边的返工上限，要么自己接手改完"
            )
            await _reject_action(session, run, ticket, state, message)
            yield protocol.ticket_updated(_ticket_payload(ticket))
            return
        ticket.state = TicketState.REWORK.value
        ticket.verdict = VERDICT_CHANGES
        ticket.review_comment = order.review_comment
        ticket.holder_key = target_node.key
        ticket.to_role_id = target_node.role_id
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.DONE:
        ticket.state = TicketState.DONE.value
        ticket.closed_at = utcnow()
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return

    if action is TicketAction.ASK_USER:
        question = order.question or "入口角色需要你补充信息"
        options = [option.to_dict() for option in order.options]
        ticket.state = TicketState.BLOCKED.value
        ticket.result = {**(ticket.result or {}), "question": question, "options": options}
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))
        yield protocol.notice(question, "ask", question=question, options=options)
        return

    if action is TicketAction.REJECT:
        ticket.state = TicketState.REJECTED.value
        ticket.review_comment = order.reason or order.review_comment
        ticket.closed_at = utcnow()
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.notice(
            f"工单 {ticket_code(ticket.id)} 被「{node.key}」否决：{ticket.review_comment or '未说明原因'}",
            "error",
        )
        yield protocol.ticket_updated(_ticket_payload(ticket))
        return


async def _safe_apply(
    session: AsyncSession,
    prepared: PreparedTurn,
    run: CollabRun,
    ticket: CollabTicket,
    node: NodeSpec,
    order,
    reviewer: str | None,
    state: TurnState,
) -> AsyncIterator[protocol.CollabEvent]:
    """执行动作；任何意外都转成一条显式错误并挂起（绝不让事件流中途断掉）。"""
    try:
        async for event in _apply_action(session, prepared, run, ticket, node, order, reviewer, state):
            yield event
    except Exception as exc:  # noqa: BLE001
        message = f"处理工单动作时出错：{exc}"
        ticket.state = TicketState.BLOCKED.value
        run.error = message
        session.add(ticket)
        session.add(run)
        await session.commit()
        yield protocol.notice(message, "error")
        state.stop, state.status, state.reason = True, RunStatus.WAITING_USER.value, message


async def _rollup_parents(session: AsyncSession, run: CollabRun) -> AsyncIterator[protocol.CollabEvent]:
    """子工单全部收尾后，把父工单交回它的发起人。"""
    tickets = await _tickets(session, run.id)
    by_parent: dict[str, list[CollabTicket]] = {}
    for ticket in tickets:
        if ticket.parent_ticket_id:
            by_parent.setdefault(ticket.parent_ticket_id, []).append(ticket)
    for ticket in tickets:
        if ticket.state != TicketState.WAITING_CHILDREN.value:
            continue
        children = by_parent.get(ticket.id, [])
        if not children or any(child.state not in FINISHED_STATES for child in children):
            continue
        ticket.state = TicketState.OPEN.value
        ticket.holder_key = ticket.owner_key or ticket.holder_key
        ticket.updated_at = utcnow()
        session.add(ticket)
        await session.commit()
        await session.refresh(ticket)
        yield protocol.ticket_updated(_ticket_payload(ticket))


async def _finalize(session: AsyncSession, prepared: PreparedTurn, state: TurnState) -> AsyncIterator[protocol.CollabEvent]:
    run = await session.get(CollabRun, prepared.run.id)
    if run is None:
        return
    tickets = await _tickets(session, run.id)
    root = next((t for t in tickets if t.parent_ticket_id is None), None)

    if state.status:
        status = state.status
    elif any(t.state == TicketState.BLOCKED.value for t in tickets):
        status = RunStatus.WAITING_USER.value
    elif root is not None and root.state == TicketState.DONE.value:
        status = RunStatus.DONE.value
    elif any(t.state in ACTIVE_STATES for t in tickets):
        status = RunStatus.WAITING_USER.value
    elif any(t.state == TicketState.DONE.value for t in tickets):
        status = RunStatus.DONE.value
    else:
        status = RunStatus.ERROR.value

    run.status = status
    run.error = state.reason if status in (RunStatus.WAITING_USER.value, RunStatus.ERROR.value) else None
    run.updated_at = utcnow()
    session.add(run)
    await session.commit()

    if state.reason:
        yield protocol.notice(state.reason, "warn" if state.budget_exhausted else "error")
    yield protocol.run_status(status, state.reason)


def _ticket_payload(ticket: CollabTicket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "run_id": ticket.run_id,
        "parent_ticket_id": ticket.parent_ticket_id,
        "from_role_id": ticket.from_role_id,
        "to_role_id": ticket.to_role_id,
        "owner_key": ticket.owner_key,
        "holder_key": ticket.holder_key,
        "submitter_key": ticket.submitter_key,
        "title": ticket.title,
        "brief": ticket.brief or {},
        "state": ticket.state,
        "round": ticket.round,
        "max_rounds": ticket.max_rounds,
        "verdict": ticket.verdict,
        "review_comment": ticket.review_comment,
        "result": ticket.result or {},
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "closed_at": ticket.closed_at.isoformat() if ticket.closed_at else None,
    }
