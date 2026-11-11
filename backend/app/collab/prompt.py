"""节点输入组装：把「岗位说明 + 工单载荷 + 本工单历史」拼成一次步骤的 prompt。

这是「不共享记忆」的落地处（需求 C5）：交给下游的**只有工单字段、一句话摘要与文件清单**，
不带任何角色的对话原文，也不带其他工单的内容。纯函数模块，可以被直接断言。

人格（``role.system_prompt``）不在这里拼 —— 它由 ``execution.py`` 按适配器的
``native_system_prompt`` 能力决定放进 system prompt 还是折进 prompt。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .schemas import Duty, NodeSpec, TicketAction, TicketState, WorkspaceScope
from .tickets import render_protocol_guide

DUTY_LABELS: dict[Duty, str] = {
    Duty.COORDINATOR: "协调者",
    Duty.ENGINEER: "实施者",
    Duty.REVIEWER: "审查者",
}

DUTY_DEFAULT_INSTRUCTIONS: dict[Duty, str] = {
    Duty.COORDINATOR: "你是这条链路的组织者：理解目标、拆解工作、把工单派给合适的下游，并对结果负责。",
    Duty.ENGINEER: "你是执行者：按工单的需求与验收标准动手完成，然后提交结果。",
    Duty.REVIEWER: "你是审查者：逐条对照验收标准检查产出，给出可执行的结论。",
}

DUTY_DEFAULT_SCOPE: dict[Duty, str] = {
    Duty.COORDINATOR: "不写代码、不改文件、不执行命令；只通过工单把任务交出去。",
    Duty.ENGINEER: "只在团队共享工作目录内改动；不得擅自扩大需求范围。",
    Duty.REVIEWER: "只读；不得修改任何文件，只产出审查意见。",
}

WORKSPACE_NOTES: dict[WorkspaceScope, str] = {
    WorkspaceScope.NONE: "没有该路径的读写权限；只依据工单信息工作，不要读取、写入或执行命令。",
    WorkspaceScope.READ: "只读；只能读取该路径内的文件，禁止创建、修改、删除，也禁止访问该路径之外的位置。",
    WorkspaceScope.WRITE: "可读写；所有读取与写入都必须在该路径内完成，禁止访问该路径之外的位置。",
}


@dataclass(slots=True)
class TicketView:
    """工单的只读快照（由 engine 从数据库读出后传入，prompt 层不碰 ORM）。"""

    id: str
    title: str
    brief: dict[str, Any] = field(default_factory=dict)
    state: str = TicketState.OPEN.value
    round: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    review_comment: str | None = None
    submitter_key: str = ""


@dataclass(slots=True)
class HistoryEntry:
    """时间线上的一条**摘要**（不是原文）。"""

    role_label: str
    action: str
    gist: str


def ticket_code(ticket_id: str) -> str:
    return f"T-{ticket_id[:8]}"


def _bullet_list(lines: Sequence[str], limit: int = 12) -> str:
    if not lines:
        return "（未指定）"
    items = [str(line).strip() for line in lines if str(line).strip()][:limit]
    if not items:
        return "（未指定）"
    return "\n".join(f"{index + 1}) {item}" for index, item in enumerate(items))


def _artifacts_text(result: dict[str, Any]) -> str:
    artifacts = result.get("artifacts") or []
    paths: list[str] = []
    for item in artifacts:
        if isinstance(item, dict):
            path = item.get("path") or item.get("name")
            if path:
                paths.append(str(path))
        elif isinstance(item, str):
            paths.append(item)
    if not paths:
        return ""
    shown = paths[:20]
    suffix = f"（共 {len(paths)} 个）" if len(paths) > len(shown) else ""
    return f"\n   - 变更文件：{'、'.join(shown)}{suffix}"


def _history_text(history: Sequence[HistoryEntry]) -> str:
    if not history:
        return "（本工单还没有历史）"
    lines = []
    for entry in history[-8:]:
        lines.append(f"  - {entry.role_label} · {entry.action}：{entry.gist}")
    return "\n".join(lines)


def build_step_prompt(
    *,
    team_name: str,
    workspace_path: str,
    node: NodeSpec,
    ticket: TicketView,
    history: Sequence[HistoryEntry],
    assignees: Sequence[str],
    reviewer: str | None,
    rework_targets: Sequence[str] = (),
    user_message: str | None = None,
    node_labels: dict[str, str] | None = None,
) -> str:
    """组装一次步骤的完整输入。"""
    labels = node_labels or {}
    duty_label = DUTY_LABELS[node.duty]
    scope = node.workspace_scope or {
        Duty.COORDINATOR: WorkspaceScope.NONE,
        Duty.ENGINEER: WorkspaceScope.WRITE,
        Duty.REVIEWER: WorkspaceScope.READ,
    }[node.duty]

    def named(key: str) -> str:
        label = labels.get(key, "")
        return f"{label}（{key}）" if label and label != key else key

    brief = ticket.brief or {}
    header = [
        f"[协作任务] 协作组「{team_name}」· 你的岗位：{duty_label}（{node.key}）",
        f"职责：{node.instructions.strip() or DUTY_DEFAULT_INSTRUCTIONS[node.duty]}",
        f"能力边界（必须遵守）：{node.capability_scope.strip() or DUTY_DEFAULT_SCOPE[node.duty]}",
        f"工作路径（本次协作唯一允许访问的目录）：{workspace_path}",
        f"路径权限（必须遵守）：{WORKSPACE_NOTES[scope]}",
        "",
        "输出协议：",
        render_protocol_guide(
            duty_label=duty_label,
            actions=node.output_contract
            or {
                Duty.COORDINATOR: (TicketAction.DELEGATE, TicketAction.ASK_USER, TicketAction.DONE, TicketAction.REJECT),
                Duty.ENGINEER: (TicketAction.SUBMIT, TicketAction.DELEGATE, TicketAction.REJECT),
                Duty.REVIEWER: (TicketAction.REVIEW, TicketAction.REJECT),
            }[node.duty],
            assignees=[named(key) for key in assignees],
            reviewer=named(reviewer) if reviewer else None,
            rework_targets=[named(key) for key in rework_targets],
        ),
    ]

    body = [
        "",
        f"—— 工单 {ticket_code(ticket.id)} ——",
        f"标题：{ticket.title}",
        f"目标：{brief.get('goal') or '（未指定）'}",
        f"需求：\n{_bullet_list(brief.get('requirements') or [])}",
        f"验收标准：\n{_bullet_list(brief.get('acceptance') or [])}",
        f"约束：\n{_bullet_list(brief.get('constraints') or [])}",
    ]
    if brief.get("deliverables"):
        body.append(f"交付物：\n{_bullet_list(brief['deliverables'])}")

    result = ticket.result or {}
    if result.get("summary") or result.get("artifacts"):
        body.append("上游交付摘要：")
        if result.get("summary"):
            body.append(f"   - {result['summary']}")
        artifacts = _artifacts_text(result)
        if artifacts:
            body.append(artifacts.lstrip("\n"))

    body.append(f"本工单历史（仅这一张工单）：\n{_history_text(history)}")
    body.append(f"本轮要求：{expected_instruction(node, ticket, user_message=user_message)}")
    return "\n".join(header) + "\n".join(body) + "\n"


def expected_instruction(node: NodeSpec, ticket: TicketView, *, user_message: str | None = None) -> str:
    """按岗位与工单当前状态，告诉它这一轮应该干什么。"""
    if ticket.state == TicketState.REVIEW.value and node.duty is Duty.REVIEWER:
        text = (
            "请审查上面这张工单的产出：逐条对照验收标准，能通过就给 "
            "review + pass；不通过就给 review + changes_requested，"
            "并在 review_comment 里写清不符合哪一条、为什么。"
        )
        return text

    if ticket.state == TicketState.REWORK.value:
        comment = (ticket.review_comment or "").strip()
        base = "这是一张被审查打回、需要返工的工单。"
        if comment:
            base += f"审查意见：{comment}"
        if node.duty is Duty.REVIEWER:
            return base
        return (
            base + " 请针对每一条意见修改；改完后用 submit 重新提交。"
            "如果你需要重新组织分工，也可以 delegate 给下游（用 split 拆子工单）。"
        )

    if node.duty is Duty.ENGINEER:
        return "请按需求动手完成，然后用 submit 提交结果（summary + artifacts）。"
    if node.duty is Duty.REVIEWER:
        return "请给出审查结论（review + verdict）。"
    if user_message:
        return (
            "用户刚刚说：" + user_message.strip() + "\n"
            "请据此更新工单：能派下去就 delegate（handoff 转交 / split 拆子工单）；"
            "信息不足就 ask_user 提问（必须带 2-6 个可点 options）；"
            "整件事都完成了用 done。"
        )
    return (
        "请推进这张工单：把任务派给合适的下游（delegate），"
        "或者确认它已经完成（done）；"
        "需要用户补充信息时用 ask_user（必须带可点 options）。"
    )
