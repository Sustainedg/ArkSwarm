"""M3 防死循环护栏：边级返工上限、拆解层数上限、重名子工单、运行预算回传。

这些护栏的产品承诺是「宁可挂起让人判断，也不静默空转烧钱」：
任何一条触发后，工单进 ``blocked``、运行进 ``waiting_user``，并且必须给出可读原因
（docs/collaboration-design.md §3.3）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

from app.collab import graph as graph_service
from app.collab.schemas import Duty, EdgeKind, EdgeSpec, GraphSpec, NodeSpec
from app.db import SessionLocal
from app.models import AgentProfile
from helpers import collab_options


ROLE_NAMES = (("ceo", "CEO"), ("cto", "CTO"), ("se", "SE"), ("qa", "审查员"))


def handoff(assignee: str, title: str) -> dict:
    return {
        "action": "delegate",
        "assignee": assignee,
        "mode": "handoff",
        "ticket": {"title": title, "goal": f"完成 {title}", "acceptance": ["按验收标准通过"]},
    }


def split(assignee: str, title: str) -> dict:
    return {
        "action": "delegate",
        "assignee": assignee,
        "mode": "split",
        "ticket": {
            "title": title,
            "goal": f"完成：{title}",
            "requirements": ["按标题把活干完"],
            "acceptance": ["验收通过"],
        },
    }


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def make_env(script: dict, tmp_path: Path) -> dict:
    """建一个假智能体 + 四个智能体；剧本决定每个岗位每次说什么。"""
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    async with SessionLocal() as session:
        anchor = AgentProfile(
            name="假协作智能体",
            type_id="custom-cli",
            options=collab_options(script_path, state_dir),
        )
        session.add(anchor)
        await session.commit()
        await session.refresh(anchor)

        roles: dict[str, str] = {}
        for key, name in ROLE_NAMES:
            role = AgentProfile(
                name=name,
                avatar="🧑",
                system_prompt=f"你是 {name}。",
                type_id="custom-cli",
                options=collab_options(script_path, state_dir),
            )
            session.add(role)
            await session.commit()
            await session.refresh(role)
            roles[key] = role.id

    return {"agent_profiles": roles, "state_dir": state_dir}


async def create_team(
    client: httpx.AsyncClient, spec: GraphSpec, budget: dict | None = None
) -> dict:
    body: dict = {"name": "护栏测试组", "entry_key": "ceo", "graph": graph_service.to_storable(spec)}
    if budget is not None:
        body["budget"] = budget
    created = await client.post("/api/collab/teams", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["validation"]["ok"], created.json()["validation"]
    return created.json()


async def start_run(client: httpx.AsyncClient, team_id: str, goal: str) -> tuple[str, list[dict]]:
    run = await client.post("/api/collab/runs", json={"team_id": team_id})
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": goal})
    assert response.status_code == 200, response.text
    return run_id, await read_sse(response)


def linear_graph(roles: dict[str, str], rework_max_rounds: int = 0) -> GraphSpec:
    """CEO → CTO → SE，SE 提交给审查员，审查员打回 SE（可带边级返工上限）。"""
    return GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=roles["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="cto", agent_profile_id=roles["cto"], label="CTO", duty=Duty.COORDINATOR),
            NodeSpec(key="se", agent_profile_id=roles["se"], label="SE", duty=Duty.ENGINEER),
            NodeSpec(key="qa", agent_profile_id=roles["qa"], label="审查员", duty=Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="se", kind=EdgeKind.REWORK, max_rounds=rework_max_rounds),
        ],
    )


async def test_edge_max_rounds_cuts_off_endless_rework(client: httpx.AsyncClient, tmp_path: Path):
    """边级返工上限必须真的生效：第 2 轮打回被拒，工单落上限、运行挂起。"""
    env = await make_env(
        {
            "ceo": [handoff("cto", "改造登录页")],
            "cto": [split("se", "实现暗色主题")],
            "se": [{"action": "submit", "summary": "已实现", "artifacts": ["frontend/src/theme.ts"]}],
            "qa": [
                {
                    "action": "review",
                    "verdict": "changes_requested",
                    "review_comment": "对比度还是不达标",
                    "rework_to": "se",
                }
            ],
        },
        tmp_path,
    )
    team = await create_team(client, linear_graph(env["agent_profiles"], rework_max_rounds=1))
    run_id, events = await start_run(client, team["id"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert "上限 1 轮" in (detail["error"] or "")

    child = next(t for t in detail["tickets"] if t["parent_ticket_id"])
    assert child["state"] == "blocked"
    assert child["round"] == 2 and child["max_rounds"] == 1  # 边级上限落到工单上，界面「返工 x/y」才准

    notices = [e for e in events if e["type"] == "collab.notice" and e.get("level") == "error"]
    assert notices, "返工超限必须发出 collab.notice error"

    # 编辑器要能读到并回改这个字段：协作组详情里的边必须原样带回 max_rounds
    saved = (await client.get(f"/api/collab/teams/{team['id']}")).json()
    rework = next(edge for edge in saved["graph"]["edges"] if edge["kind"] == "rework")
    assert rework["max_rounds"] == 1


async def test_ticket_depth_guard_stops_nested_splitting(client: httpx.AsyncClient, tmp_path: Path):
    """拆到第 5 层还继续拆 → 拒绝，并明确提示改用 handoff。"""
    chain = ["ceo", "e1", "e2", "e3", "e4", "e5", "e6"]
    env = await make_env(
        {
            "ceo": [split("e1", "第 1 层")],
            "e1": [split("e2", "第 2 层")],
            "e2": [split("e3", "第 3 层")],
            "e3": [split("e4", "第 4 层")],
            "e4": [split("e5", "第 5 层")],
            "e5": [split("e6", "第 6 层")],
        },
        tmp_path,
    )
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=env["agent_profiles"]["ceo"], label="CEO", duty=Duty.COORDINATOR),
            *[
                NodeSpec(key=key, agent_profile_id=env["agent_profiles"]["se"], label=key.upper(), duty=Duty.ENGINEER)
                for key in chain[1:]
            ],
        ],
        edges=[
            EdgeSpec(from_key=src, to_key=dst, kind=EdgeKind.ASSIGN)
            for src, dst in zip(chain, chain[1:])
        ],
    )
    team = await create_team(client, spec)
    run_id, events = await start_run(client, team["id"], "拆一个很深的活")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    error = detail["error"] or ""
    assert "第 5 层" in error and "handoff" in error
    # 根工单 + 5 层子工单：第 6 层被挡住，没有建出来（工单数 = 链上除末端的节点数）
    assert detail["tickets_used"] == len(chain) - 1
    blocked = [t for t in detail["tickets"] if t["state"] == "blocked"]
    assert len(blocked) == 1 and blocked[0]["title"] == "第 5 层"
    assert any(e["type"] == "collab.notice" and e.get("level") == "error" for e in events)


async def test_duplicate_child_titles_guard(client: httpx.AsyncClient, tmp_path: Path):
    """同一个父工单下第三次拆出同名子工单 → 拒绝，并提示换个更具体的标题。"""
    env = await make_env(
        {
            "ceo": [split("se", "写登录接口"), split("se", "写登录接口"), split("se", "写登录接口")],
            "se": [{"action": "submit", "summary": "写好了"}],
        },
        tmp_path,
    )
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=env["agent_profiles"]["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="se", agent_profile_id=env["agent_profiles"]["se"], label="SE", duty=Duty.ENGINEER),
        ],
        edges=[EdgeSpec(from_key="ceo", to_key="se", kind=EdgeKind.ASSIGN)],
    )
    team = await create_team(client, spec)
    run_id, _ = await start_run(client, team["id"], "把登录接口写了")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert "同名" in (detail["error"] or "")
    named = [t for t in detail["tickets"] if t["title"] == "写登录接口"]
    assert len(named) == 2  # 前两张建出来了，第三张被挡住
    assert detail["tickets_used"] == 3  # 根工单 + 两张子工单


async def test_duplicate_child_titles_guard_catches_tail_tweaks(client: httpx.AsyncClient, tmp_path: Path):
    """只改标题尾巴（「并回报」之类）绕开重名护栏也要被拦住——这是线上真实发生过的空转形态。"""
    env = await make_env(
        {
            "ceo": [
                split("se", "写登录接口"),
                split("se", "写登录接口并回报"),
                split("se", "写登录接口并回报结果"),
            ],
            "se": [{"action": "submit", "summary": "写好了"}],
        },
        tmp_path,
    )
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=env["agent_profiles"]["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="se", agent_profile_id=env["agent_profiles"]["se"], label="SE", duty=Duty.ENGINEER),
        ],
        edges=[EdgeSpec(from_key="ceo", to_key="se", kind=EdgeKind.ASSIGN)],
    )
    team = await create_team(client, spec)
    run_id, _ = await start_run(client, team["id"], "把登录接口写了")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert "同名" in (detail["error"] or "")
    assert detail["tickets_used"] == 3  # 根工单 + 前两张（第三张被挡）
    titles = {ticket["title"] for ticket in detail["tickets"] if ticket["parent_ticket_id"]}
    assert titles == {"写登录接口", "写登录接口并回报"}


async def test_run_detail_reports_budget(client: httpx.AsyncClient, tmp_path: Path):
    """运行详情要带上本次预算，界面才能显示「已用 / 上限」。"""
    env = await make_env({"ceo": [handoff("cto", "随便干点啥")]}, tmp_path)
    budget = {
        "max_steps": 7,
        "max_tickets": 4,
        "max_rounds_per_ticket": 2,
        "max_tokens": 0,
        "timeout_seconds": 600,
    }
    team = await create_team(client, linear_graph(env["agent_profiles"]), budget=budget)
    run_id, _ = await start_run(client, team["id"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["budget"] == budget
    listed = (await client.get("/api/collab/runs", params={"team_id": team["id"]})).json()["items"]
    assert listed and listed[0]["budget"]["max_steps"] == 7
