"""协作引擎端到端：用替身智能体跑通「派发 → 拆解 → 实施 → 审查 → 打回 → 复审 → 完成」。

这些用例同时守住两件最要紧的承诺：

* 角色之间**只传工单**（下游的 prompt 里不会出现任何角色的对话原文）；
* 任何异常/不符合协议都**显式挂起**（``waiting_user``），并且能重试续跑。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import pytest_asyncio

from app.collab.schemas import Duty, EdgeKind, EdgeSpec, GraphSpec, NodeSpec
from app.collab import graph as graph_service
from app.db import SessionLocal
from app.models import AgentInstance, Role

FAKE_AGENT = str(Path(__file__).parent / "fakes" / "fake_collab_agent.py")

#: 每个角色都会说的一句口头禅；它出现在别人的输入里就说明「共享了对话记忆」
CHITCHAT = "我来处理"

HANDOFF = {
    "action": "delegate",
    "assignee": "cto",
    "mode": "handoff",
    "ticket": {
        "title": "登录页改造为暗色主题",
        "goal": "在不改动后端的前提下支持暗色主题",
        "requirements": ["新增主题开关", "持久化用户选择"],
        "acceptance": ["刷新后主题保持", "对比度符合 WCAG AA"],
        "constraints": ["只改 frontend/src/**", "不得新增依赖"],
    },
}

FULL_SCRIPT = {
    "ceo": [HANDOFF],
    "cto": [
        {
            "action": "delegate",
            "assignee": "se",
            "mode": "split",
            "ticket": {
                "title": "实现主题开关与持久化",
                "goal": "按验收标准实现暗色主题",
                "requirements": ["主题开关", "localStorage 持久化"],
                "acceptance": ["刷新后主题保持"],
            },
        },
        {"action": "done", "remark": "子任务已全部通过，汇总完成"},
    ],
    "se": [
        {"action": "submit", "summary": "已完成主题开关与持久化", "artifacts": ["frontend/src/theme.ts"]},
        {
            "action": "submit",
            "summary": "已按审查意见补上对比度",
            "artifacts": ["frontend/src/theme.ts", "frontend/src/index.css"],
        },
    ],
    "qa": [
        {
            "action": "review",
            "verdict": "changes_requested",
            "review_comment": "第 2 条验收未满足：刷新后主题没有保持",
            "rework_to": "se",
        },
        {"action": "review", "verdict": "pass"},
    ],
}


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def make_scenario(script: dict, tmp_path: Path) -> dict:
    """造好「一个假智能体 + 四个角色 + 剧本文件」。"""
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    async with SessionLocal() as session:
        agent = AgentInstance(
            name="假协作智能体",
            type_id="custom-cli",
            options={
                "command": sys.executable,
                "args": [FAKE_AGENT, str(script_path), str(state_dir)],
                "prompt_mode": "stdin",
                "output_format": "jsonl",
                "text_path": "delta",
                "delta_mode": True,
                "session_path": "session_id",
                "usage_path": "usage",
            },
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)

        roles = {}
        for key, name in (("ceo", "CEO"), ("cto", "CTO"), ("se", "SE"), ("qa", "审查员")):
            role = Role(
                name=name,
                avatar="🧑",
                system_prompt=f"你是 {name}。",
                agent_id=agent.id,
            )
            session.add(role)
            await session.commit()
            await session.refresh(role)
            roles[key] = role.id

    return {
        "agent_id": agent.id,
        "roles": roles,
        "state_dir": state_dir,
        "script_path": script_path,
    }


def team_body(roles: dict[str, str]) -> dict:
    """CEO → CTO → SE，SE 提交给审查员，审查员可直接打回 SE（也可交回 CTO）。"""
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", role_id=roles["ceo"], label="CEO", duty=Duty.COORDINATOR,
                     instructions="只负责理解需求与派活，不写代码",
                     capability_scope="不得写代码、不得访问工作目录"),
            NodeSpec(key="cto", role_id=roles["cto"], label="CTO", duty=Duty.COORDINATOR,
                     instructions="负责拆解任务与汇总"),
            NodeSpec(key="se", role_id=roles["se"], label="SE", duty=Duty.ENGINEER),
            NodeSpec(key="qa", role_id=roles["qa"], label="审查员", duty=Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="se", kind=EdgeKind.REWORK),
        ],
    )
    return {
        "name": "客户端研发组",
        "entry_key": "ceo",
        "graph": graph_service.to_storable(spec),
    }


async def start_run(client: httpx.AsyncClient, roles: dict[str, str], goal: str) -> tuple[str, list[dict]]:
    created = await client.post("/api/collab/teams", json=team_body(roles))
    assert created.status_code == 201, created.text
    assert created.json()["validation"]["ok"], created.json()["validation"]
    run = await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": goal})
    assert response.status_code == 200, response.text
    return run_id, await read_sse(response)


def prompts(state_dir: Path) -> list[str]:
    path = state_dir / "prompts.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["prompt"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


#: 岗位头行（如「[协作任务] … 你的岗位：实施者（se）」）；节点 key 也会作为别人的历史摘要出现，
#: 所以只认这一行，不认正文里的 key。
POST_LINE = re.compile(r"\[协作任务\][^\n]*你的岗位：[^（\n]*（(?P<key>[^）\n]+)）")


def prompts_of(all_prompts: list[str], key: str) -> list[str]:
    """挑出**这一轮就是发给该岗位**的 prompt（按岗位头判定，不看正文里的 key）。"""
    picked = []
    for text in all_prompts:
        match = POST_LINE.search(text)
        if match and match.group("key") == key:
            picked.append(text)
    return picked


@pytest_asyncio.fixture
async def full(client: httpx.AsyncClient, tmp_path: Path) -> dict:
    return await make_scenario(FULL_SCRIPT, tmp_path)


async def test_full_chain_with_one_rework_round(client: httpx.AsyncClient, full):
    run_id, events = await start_run(client, full["roles"], "把登录页改成暗色主题")

    kinds = [event["type"] for event in events]
    # 串行推进：每一步都有 started / finished，最后给出运行状态
    assert kinds[0] == "collab.run.started"
    assert kinds.count("collab.step.started") == 7, kinds
    assert kinds.count("collab.step.finished") == 7
    # 流以框架自带的 stream.closed 收尾；运行态看最后一个 collab.run.status
    assert kinds[-1] == "stream.closed"
    statuses = [event["status"] for event in events if event["type"] == "collab.run.status"]
    assert statuses and statuses[-1] == "done"
    assert "collab.ticket.created" in kinds  # CTO 拆出的子工单

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done"
    assert detail["tickets_used"] == 2
    assert detail["tickets"][0]["state"] == "done"
    # 审查打回后重走了一轮：子工单的 round 记到了 1
    child = next(t for t in detail["tickets"] if t["parent_ticket_id"])
    assert child["state"] == "done" and child["round"] == 1 and child["verdict"] == "pass"

    # 时间线：用户 1 条 + 角色 7 条，且顺序与岗位一致
    assert [s["role_key"] for s in detail["steps"]] == ["user", "ceo", "cto", "se", "qa", "se", "qa", "cto"]
    assert detail["steps"][0]["content"] == "把登录页改成暗色主题"
    # 用量从每一步累加到运行上
    assert detail["usage"]["input_tokens"] == 70 and detail["usage"]["output_tokens"] == 35


async def test_downstream_only_sees_the_ticket_never_the_conversation(client: httpx.AsyncClient, full):
    """C5：下游只拿到工单字段与摘要，拿不到任何角色的对话原文。"""
    await start_run(client, full["roles"], "把登录页改成暗色主题")
    all_prompts = prompts(full["state_dir"])
    assert len(all_prompts) == 7

    for text in all_prompts:
        # 每个角色都会说「（<key>）我来处理。」；它出现在别人的输入里就等于泄露了对话
        assert CHITCHAT not in text, "下游 prompt 里出现了上游角色的对话原文"
        assert "—— 工单 T-" in text  # 交接物一定是工单

    se_prompts = prompts_of(all_prompts, "se")
    assert len(se_prompts) == 2
    # 返工那一轮带的是审查意见（工单字段），不是审查者的整段对话
    assert "审查意见：第 2 条验收未满足：刷新后主题没有保持" in se_prompts[1]
    # 实施者的输入里没有 CEO 那份需求的完整原文，只有 CTO 拆出来的子工单字段
    assert "不得新增依赖" not in se_prompts[0]
    assert "实现主题开关与持久化" in se_prompts[0]

    qa_prompts = prompts_of(all_prompts, "qa")
    assert qa_prompts and all("刷新后主题保持" in text for text in qa_prompts)

    # 协调者拿不到工作目录，也就没有代码可写
    ceo_prompts = prompts_of(all_prompts, "ceo")
    assert len(ceo_prompts) == 1
    ceo_prompt = ceo_prompts[0]
    assert "本次没有给你工作目录" in ceo_prompt
    assert "不得写代码" in ceo_prompt


async def test_protocol_violation_hangs_then_recovers_by_retry(client: httpx.AsyncClient, tmp_path: Path):
    script = {"ceo": ["我这就去办，稍等。", HANDOFF], "cto": [{"action": "done", "remark": "完成"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["roles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert "工单协议" in (detail["error"] or "")
    assert any(event["type"] == "collab.notice" and event.get("level") == "error" for event in events)

    failed = detail["steps"][-1]
    assert failed["status"] == "error" and failed["role_key"] == "ceo"

    # 修好之后「重试 + 继续」应当能接着跑完
    retried = await client.post(f"/api/collab/steps/{failed['id']}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "waiting_user"
    resumed = await client.post(f"/api/collab/runs/{run_id}/resume")
    assert resumed.status_code == 200
    resumed_events = await read_sse(resumed)
    resumed_status = [e["status"] for e in resumed_events if e["type"] == "collab.run.status"]
    assert resumed_status and resumed_status[-1] == "done"
    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done" and final["tickets"][0]["state"] == "done"


async def test_entry_can_ask_the_user_and_then_continue(client: httpx.AsyncClient, tmp_path: Path):
    script = {
        "ceo": [{"action": "ask_user", "question": "暗色主题要跟随系统设置吗？"}, HANDOFF],
        "cto": [{"action": "done", "remark": "完成"}],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["roles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    assert any(event["type"] == "collab.notice" and event.get("level") == "ask" for event in events)
    root = detail["tickets"][0]
    assert root["state"] == "blocked" and "跟随系统" in root["result"]["question"]

    # 用户回答 → 工单回到入口角色手里，继续推进到完成
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "要，跟随系统"})
    assert response.status_code == 200
    await read_sse(response)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert [s["role_key"] for s in final["steps"]][:3] == ["user", "ceo", "user"]
    assert final["steps"][2]["content"] == "要，跟随系统"


async def test_cannot_start_twice_and_resume_needs_something_to_do(client: httpx.AsyncClient, full):
    created = await client.post("/api/collab/teams", json=team_body(full["roles"]))
    team_id = created.json()["id"]
    run = await client.post("/api/collab/runs", json={"team_id": team_id})
    run_id = run.json()["id"]

    # 还没有目标就继续 → 明确拒绝
    blocked = await client.post(f"/api/collab/runs/{run_id}/resume")
    assert blocked.status_code == 409
    assert "请先告诉入口角色" in blocked.json()["detail"]

    # 图不合法时不允许开跑
    broken = team_body(full["roles"])
    broken["entry_key"] = "se"
    await client.put(f"/api/collab/teams/{team_id}", json={"entry_key": "se"})
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "开始"})
    assert response.status_code == 409
    assert "编排图还不能跑" in response.json()["detail"]
    assert broken["entry_key"] == "se"
