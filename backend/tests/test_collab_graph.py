"""编排图校验：能跑起来的硬约束 + 提醒类的软约束。"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.collab import graph as g
from app.collab.schemas import (
    BudgetSpec,
    Duty,
    EdgeKind,
    EdgePort,
    EdgeSpec,
    GraphSpec,
    NodeSpec,
    TeamCreate,
    TeamUpdate,
    TicketAction,
    WorkspaceScope,
)

ROLES = {"r-ceo", "r-cto", "r-se", "r-qa"}


def node(key: str, role: str, duty: Duty, **kw) -> NodeSpec:
    return NodeSpec(key=key, agent_profile_id=role, duty=duty, label=key, **kw)


def canonical() -> GraphSpec:
    """CEO → CTO → SE，SE 提交给 QA，QA 打回 SE：需求里的那条主链。"""
    return GraphSpec(
        nodes=[
            node("ceo", "r-ceo", Duty.COORDINATOR),
            node("cto", "r-cto", Duty.COORDINATOR),
            node("se", "r-se", Duty.ENGINEER),
            node("qa", "r-qa", Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="se", kind=EdgeKind.REWORK),
        ],
    )


def codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def warn_codes(result) -> set[str]:
    return {issue.code for issue in result.warnings}


def test_canonical_flow_is_clean():
    result = g.validate_graph(canonical(), "ceo", ROLES)
    assert result.ok, [issue.message for issue in result.errors]
    assert result.warnings == [], [issue.message for issue in result.warnings]


def test_empty_graph_and_missing_entry():
    assert codes(g.validate_graph(GraphSpec(), "ceo")) == {"graph.empty"}
    graph = canonical()
    assert "entry.missing" in codes(g.validate_graph(graph, None))
    assert "entry.not_found" in codes(g.validate_graph(graph, "nobody"))
    assert "entry.duty" in codes(g.validate_graph(graph, "se"))


def test_orphan_node_and_unreachable_warning():
    graph = canonical()
    graph.edges = [e for e in graph.edges if e.to_key != "cto"]
    result = g.validate_graph(graph, "ceo")
    assert "node.orphan" in codes(result)
    assert "node.unreachable" in warn_codes(result)


def test_review_edge_requires_rework_back_edge():
    graph = canonical()
    graph.edges = [e for e in graph.edges if e.kind is not EdgeKind.REWORK]
    result = g.validate_graph(graph, "ceo")
    assert "edge.review_without_rework" in codes(result)
    assert "node.reviewer_no_rework" in warn_codes(result)


def test_rework_edge_requires_review_edge():
    graph = canonical()
    graph.edges.append(EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.REWORK, id="cto-rework"))
    assert "edge.rework_without_review" in codes(g.validate_graph(graph, "ceo", ROLES))


def test_rework_may_go_back_to_an_upper_node():
    """需求里的那条路径：审查意见交回 CTO，由 CTO 再派给 SE。"""
    graph = canonical()
    graph.edges.append(EdgeSpec(from_key="qa", to_key="cto", kind=EdgeKind.REWORK, id="qa-to-cto"))
    result = g.validate_graph(graph, "ceo", ROLES)
    assert result.ok, [issue.message for issue in result.errors]
    assert "edge.rework_target_not_submitter" in warn_codes(result)
    assert "node.multiple_rework_targets" in warn_codes(result)
    assert g.rework_targets(graph, "qa") == ["cto", "se"]


def test_single_rework_to_upper_node_needs_no_rework_to():
    """审查者只有一条退路时，引擎按这条边自动打回，不应提示还必须填 rework_to。"""
    graph = canonical()
    graph.edges = [e for e in graph.edges if not (e.kind is EdgeKind.REWORK and e.from_key == "qa")]
    graph.edges.append(EdgeSpec(from_key="qa", to_key="cto", kind=EdgeKind.REWORK, id="qa-to-cto"))
    result = g.validate_graph(graph, "ceo", ROLES)
    assert result.ok, [issue.message for issue in result.errors]
    assert result.warnings == [], [issue.message for issue in result.warnings]
    assert g.rework_targets(graph, "qa") == ["cto"]


def test_assign_cycle_is_rejected_but_review_loop_is_allowed():
    graph = canonical()
    graph.edges.append(EdgeSpec(from_key="se", to_key="cto", kind=EdgeKind.ASSIGN))
    graph.edges.append(EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.ASSIGN, id="dup-direction"))
    result = g.validate_graph(graph, "ceo", ROLES)
    assert "edge.assign_cycle" in codes(result)
    # review/rework 构成的闭环是必须的，不能被当成环
    assert g.validate_graph(canonical(), "ceo", ROLES).ok


def test_duty_mismatch_dangling_and_self_loop():
    graph = GraphSpec(
        nodes=[node("ceo", "r-ceo", Duty.COORDINATOR), node("qa", "r-qa", Duty.REVIEWER)],
        edges=[
            EdgeSpec(from_key="ceo", to_key="qa", kind=EdgeKind.REVIEW),  # 协调者不能提交审查
            EdgeSpec(from_key="qa", to_key="ghost", kind=EdgeKind.REWORK),  # 悬空
            EdgeSpec(from_key="qa", to_key="qa", kind=EdgeKind.REWORK),  # 自环
            EdgeSpec(from_key="qa", to_key="ceo", kind=EdgeKind.ASSIGN, id="q2c"),  # 审查者不能派发
        ],
    )
    result = g.validate_graph(graph, "ceo", ROLES)
    assert {"edge.duty_mismatch", "edge.dangling", "edge.self_loop"} <= codes(result)


def test_duplicate_keys_ids_self_loop_and_no_engineer():
    graph = GraphSpec(
        nodes=[node("ceo", "r-ceo", Duty.COORDINATOR), node("ceo", "r-cto", Duty.COORDINATOR)],
        edges=[
            EdgeSpec(from_key="ceo", to_key="ceo", kind=EdgeKind.ASSIGN, id="same"),
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN, id="same"),
        ],
    )
    result = g.validate_graph(graph, "ceo", ROLES)
    assert {"node.duplicate_key", "edge.duplicate_id", "graph.no_engineer"} <= codes(result)


def test_unknown_role_is_reported_only_when_roles_are_known():
    graph = canonical()
    graph.nodes[3].agent_profile_id = "r-deleted"
    assert "node.agent_missing" in codes(g.validate_graph(graph, "ceo", ROLES))
    # 不注入智能体集合时不做这项判断（纯函数不碰数据库）
    assert "node.agent_missing" not in codes(g.validate_graph(graph, "ceo"))


def test_write_scope_on_non_engineer_is_only_a_warning():
    graph = canonical()
    graph.nodes[3].workspace_scope = WorkspaceScope.WRITE
    result = g.validate_graph(graph, "ceo", ROLES)
    assert result.ok
    assert "node.write_on_readonly_duty" in warn_codes(result)


def test_helpers_resolve_flow_and_effective_defaults():
    graph = canonical()
    assert g.allowed_assignees(graph, "ceo") == ["cto"]
    assert g.reviewer_of(graph, "se") == "qa"
    assert g.rework_targets(graph, "qa") == ["se"]
    assert g.reviewer_of(graph, "ceo") is None
    assert g.entry_node(graph, "se").duty is Duty.ENGINEER

    ceo = graph.nodes[0]
    assert g.effective_workspace_scope(ceo) is WorkspaceScope.NONE
    assert g.effective_workspace_scope(graph.nodes[3]) is WorkspaceScope.READ
    assert TicketAction.ASK_USER in g.effective_output_contract(ceo, is_entry=True)
    assert g.effective_output_contract(graph.nodes[3], is_entry=False) == (
        TicketAction.REVIEW,
        TicketAction.ESCALATE,
        TicketAction.REJECT,
    )

    budget = BudgetSpec(max_rounds_per_ticket=3)
    assert g.effective_max_rounds(graph.nodes[2], budget) == 3
    assert g.effective_max_rounds(node("se", "r-se", Duty.ENGINEER, max_rounds=5), budget) == 3


def test_non_entry_ask_user_is_warned_and_filtered():
    """非入口节点勾选 ask_user 时必须提示，并在生效动作里被剔除。

    真实事故：编辑器允许把 ask_user 勾给实施者，提示词也照写，模型于是用它向用户提问，
    运行时却被判「只有入口节点能向用户提问」。这里把「配置 → 提示词 → 校验」拉回一致。
    """
    graph = canonical()
    cto = graph.nodes[1]  # 非入口的协调者
    cto.output_contract = [TicketAction.DELEGATE, TicketAction.ASK_USER, TicketAction.DONE]

    result = g.validate_graph(graph, "ceo", ROLES)
    assert result.ok, [issue.message for issue in result.errors]
    assert "node.ask_user_not_entry" in warn_codes(result)

    assert TicketAction.ASK_USER in g.effective_output_contract(cto, is_entry=True)
    filtered = g.effective_output_contract(cto, is_entry=False)
    assert TicketAction.ASK_USER not in filtered
    assert TicketAction.DELEGATE in filtered and TicketAction.DONE in filtered


def test_escalated_contract_drops_delegate():
    """上抛件只能「解决 / 继续上报 / 交给用户」，不许再往下派。

    这条收窄必须由 ``effective_output_contract`` 统一给出，提示词与运行时校验才会一致
    （真实事故：CTO 把「环境跑不了」改写成「静态核对」又派给码农，白转一轮）。
    """
    graph = canonical()
    cto = graph.nodes[1]
    normal = g.effective_output_contract(cto, is_entry=False)
    assert TicketAction.DELEGATE in normal and TicketAction.ESCALATE in normal

    escalated = g.effective_output_contract(cto, is_entry=False, escalated=True)
    assert TicketAction.DELEGATE not in escalated
    assert TicketAction.ESCALATE in escalated


def test_escalate_is_always_available_for_non_entry_nodes():
    """上报是保底出路：节点契约里没写它也必须生效，否则历史团队里会留着一条死路。

    真实事故里 UI 节点的契约是 ``["done", "ask_user"]``；修完之后它必须拿得到 ``escalate``，
    否则用户还得逐个节点手动补勾，问题只解决了一半。
    """
    graph = canonical()
    se = graph.nodes[2]
    se.output_contract = [TicketAction.DONE, TicketAction.ASK_USER]

    contract = g.effective_output_contract(se, is_entry=False)
    assert TicketAction.ESCALATE in contract
    assert TicketAction.ASK_USER not in contract and TicketAction.DONE in contract

    # 入口节点是最顶端：没有更上级可报，由 ask_user 收口，所以不给 escalate
    graph.nodes[0].output_contract = [TicketAction.DELEGATE, TicketAction.ESCALATE]
    assert TicketAction.ESCALATE not in g.effective_output_contract(graph.nodes[0], is_entry=True)


def test_storable_round_trip():
    graph = canonical()
    data = g.to_storable(graph)
    assert isinstance(data["nodes"][0]["duty"], str)
    assert g.from_storable(data) == graph
    assert g.budget_from_storable({"max_steps": 3}).max_steps == 3


def test_edge_ports_round_trip_without_changing_semantics():
    graph = canonical()
    graph.edges[0].from_side = EdgePort.BOTTOM
    graph.edges[0].to_side = EdgePort.TOP
    stored = g.to_storable(graph)
    assert stored["edges"][0]["from_side"] == "bottom"
    assert stored["edges"][0]["to_side"] == "top"
    restored = g.from_storable(stored)
    assert restored.edges[0].from_side is EdgePort.BOTTOM
    assert restored.edges[0].to_side is EdgePort.TOP
    assert g.validate_graph(restored, "ceo", ROLES).ok


def test_workspace_dir_must_be_absolute_and_blank_means_default():
    assert TeamCreate(name="测试组", workspace_dir="  ").workspace_dir is None
    assert TeamUpdate(workspace_dir="").workspace_dir is None
    assert TeamCreate(name="测试组", workspace_dir="~/project").workspace_dir == str(
        Path("~/project").expanduser()
    )
    with pytest.raises(ValidationError):
        TeamCreate(name="测试组", workspace_dir="relative/path")
    with pytest.raises(ValidationError):
        TeamUpdate(workspace_dir="relative/path")
