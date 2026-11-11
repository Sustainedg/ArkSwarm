"""协作引擎端到端：用替身智能体跑通「派发 → 拆解 → 实施 → 审查 → 打回 → 复审 → 完成」。

这些用例同时守住两件最要紧的承诺：

* 角色之间**只传工单**（下游的 prompt 里不会出现任何角色的对话原文）；
* 任何异常/不符合协议都**显式挂起**（``waiting_user``），并且能重试续跑。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest_asyncio
from sqlmodel import select

from app.collab.bus import bus
from app.collab.schemas import Duty, EdgeKind, EdgeSpec, GraphSpec, NodeSpec
from app.collab import graph as graph_service
from app.collab.store import recover_interrupted
from app.collab.tables import CollabRun, CollabStep
from app.db import SessionLocal
from app.models import AgentInstance, Role, utcnow

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


def team_body(roles: dict[str, str], workspace_dir: str | None = None) -> dict:
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
    body = {
        "name": "客户端研发组",
        "entry_key": "ceo",
        "graph": graph_service.to_storable(spec),
    }
    if workspace_dir:
        body["workspace_dir"] = workspace_dir
    return body


async def start_run(
    client: httpx.AsyncClient,
    roles: dict[str, str],
    goal: str,
    *,
    team_workspace_dir: str | None = None,
    run_workspace_dir: str | None = None,
) -> tuple[str, list[dict]]:
    created = await client.post("/api/collab/teams", json=team_body(roles, team_workspace_dir))
    assert created.status_code == 201, created.text
    assert created.json()["validation"]["ok"], created.json()["validation"]
    run_body: dict = {"team_id": created.json()["id"]}
    if run_workspace_dir:
        run_body["workspace_dir"] = run_workspace_dir
    run = await client.post("/api/collab/runs", json=run_body)
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
#: prompt 里的团队工作路径行；所有岗位必须拿到同一条路径。
WORKSPACE_LINE = re.compile(r"工作路径（本次协作唯一允许访问的目录）：(.+)")


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


async def test_single_rework_to_cto_does_not_need_rework_to(client: httpx.AsyncClient, tmp_path: Path):
    """审查者只有一条 QA → CTO 退路时，工单不写 rework_to 也应自动交回 CTO。"""
    script = {
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
            {
                "action": "delegate",
                "assignee": "se",
                "mode": "handoff",
                "ticket": {"title": "按审查意见返工"},
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
            },
            {"action": "review", "verdict": "pass"},
        ],
    }
    scenario = await make_scenario(script, tmp_path)
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", role_id=scenario["roles"]["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="cto", role_id=scenario["roles"]["cto"], label="CTO", duty=Duty.COORDINATOR),
            NodeSpec(key="se", role_id=scenario["roles"]["se"], label="SE", duty=Duty.ENGINEER),
            NodeSpec(key="qa", role_id=scenario["roles"]["qa"], label="审查员", duty=Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="cto", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="cto", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="cto", kind=EdgeKind.REWORK),
        ],
    )
    created = await client.post(
        "/api/collab/teams",
        json={"name": "交回 CTO 的研发组", "entry_key": "ceo", "graph": graph_service.to_storable(spec)},
    )
    assert created.status_code == 201, created.text
    validation = created.json()["validation"]
    assert validation["ok"] and not validation["warnings"], validation

    run = await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})
    assert run.status_code == 201, run.text
    response = await client.post(
        f"/api/collab/runs/{run.json()['id']}/messages", json={"content": "把登录页改成暗色主题"}
    )
    assert response.status_code == 200, response.text
    await read_sse(response)

    detail = (await client.get(f"/api/collab/runs/{run.json()['id']}")).json()
    assert detail["status"] == "done"
    assert [step["role_key"] for step in detail["steps"]] == [
        "user", "ceo", "cto", "se", "qa", "cto", "se", "qa", "cto",
    ]
    child = next(ticket for ticket in detail["tickets"] if ticket["parent_ticket_id"])
    assert child["state"] == "done" and child["round"] == 1 and child["verdict"] == "pass"


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

    # 协调者也会收到团队工作路径；但按岗位职责没有该路径的读写权限
    ceo_prompts = prompts_of(all_prompts, "ceo")
    assert len(ceo_prompts) == 1
    ceo_prompt = ceo_prompts[0]
    assert "工作路径（本次协作唯一允许访问的目录）：" in ceo_prompt
    assert "没有该路径的读写权限" in ceo_prompt
    assert "不得写代码" in ceo_prompt


async def test_run_workspace_path_is_passed_to_every_role(client: httpx.AsyncClient, full, tmp_path: Path):
    """新建协作任务时指定的工作路径必须原样传给每个岗位，并落在运行记录里。"""
    shared = (tmp_path / "shared-project").resolve()
    shared.mkdir()

    run_id, _ = await start_run(client, full["roles"], "统一工作路径", run_workspace_dir=str(shared))
    all_prompts = prompts(full["state_dir"])
    assert len(all_prompts) == 7

    matched = [WORKSPACE_LINE.search(text) for text in all_prompts]
    assert all(match is not None for match in matched)
    assert {match.group(1).strip() for match in matched if match} == {str(shared)}

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["workspace_dir"] == str(shared)


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


async def test_round_with_two_work_order_blocks_uses_the_last_one(client: httpx.AsyncClient, tmp_path: Path):
    """工具型内核会把草稿和结论写进同一轮：取最后一个并提示，而不是把整轮判死。"""
    draft = (
        "先写个草稿工单：\n\n"
        '```work_order\n{"action": "ask_user", "question": "先问一句", '
        '"options": [{"label": "好", "value": "好"}, {"label": "不好", "value": "不好"}]}\n```\n'
    )
    final = "\n想清楚了，最终决定如下：\n\n```work_order\n" + json.dumps(HANDOFF, ensure_ascii=False) + "\n```\n"
    script = {"ceo": [draft + final], "cto": [{"action": "done", "remark": "完成"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["roles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done"
    ceo_step = detail["steps"][1]
    assert ceo_step["role_key"] == "ceo" and ceo_step["status"] == "complete"
    assert ceo_step["action"] == "delegate" and ceo_step["target_key"] == "cto"
    warnings = [e for e in events if e["type"] == "collab.notice" and e.get("level") == "warn"]
    assert any("2 个 work_order" in (e.get("text") or "") for e in warnings)
    # 草稿块不进正文：时间线上不该出现「假装发出去」的工单
    assert "work_order" not in ceo_step["content"]


async def test_entry_can_ask_the_user_and_then_continue(client: httpx.AsyncClient, tmp_path: Path):
    script = {
        "ceo": [
            {
                "action": "ask_user",
                "question": "暗色主题要跟随系统设置吗？",
                "options": [
                    {"label": "跟随系统", "value": "要，跟随系统", "description": "系统切换时同步"},
                    {"label": "固定暗色", "value": "不要，固定暗色"},
                ],
            },
            HANDOFF,
        ],
        "cto": [{"action": "done", "remark": "完成"}],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["roles"], "把登录页改成暗色主题")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    ask_events = [event for event in events if event["type"] == "collab.notice" and event.get("level") == "ask"]
    assert ask_events
    assert ask_events[0]["question"] == "暗色主题要跟随系统设置吗？"
    assert [item["label"] for item in ask_events[0]["options"]] == ["跟随系统", "固定暗色"]
    root = detail["tickets"][0]
    assert root["state"] == "blocked" and "跟随系统" in root["result"]["question"]
    assert root["result"]["options"][0] == {
        "label": "跟随系统",
        "value": "要，跟随系统",
        "description": "系统切换时同步",
        "freeform": False,
    }

    # 用户回答 → 工单回到入口角色手里，继续推进到完成
    response = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "要，跟随系统"})
    assert response.status_code == 200
    await read_sse(response)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert [s["role_key"] for s in final["steps"]][:3] == ["user", "ceo", "user"]
    assert final["steps"][2]["content"] == "要，跟随系统"
    # 答复之后旧卡片数据被清掉，后续因别的原因挂起时不会误显示上一轮的选择题
    assert "question" not in final["tickets"][0]["result"]
    assert "options" not in final["tickets"][0]["result"]


async def test_ask_user_without_options_is_salvaged_into_card(client: httpx.AsyncClient, tmp_path: Path):
    """模型漏给 options 时仍应给出可点卡片，而不是只报协议错误。"""
    script = {"ceo": [{"action": "ask_user", "question": "暗色还是亮色？"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, events = await start_run(client, scenario["roles"], "把登录页换一套配色")

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "waiting_user"
    root = detail["tickets"][0]
    assert root["state"] == "blocked"
    assert root["result"]["question"] == "暗色还是亮色？"
    labels = [item["label"] for item in root["result"]["options"]]
    assert labels[:2] == ["暗色", "亮色"]

    ask_events = [event for event in events if event["type"] == "collab.notice" and event.get("level") == "ask"]
    assert ask_events and ask_events[0]["options"] == root["result"]["options"]


async def test_resume_after_created_at_exceeds_timeout(client: httpx.AsyncClient, tmp_path: Path):
    """运行创建超过时限后，用户回来继续仍应获得一个新的推进窗口。"""
    ask_once = {
        "action": "ask_user",
        "question": "先做哪一部分？",
        "options": [
            {"label": "先做核心流程", "value": "先做核心流程"},
            {"label": "我来说明", "value": "", "freeform": True},
        ],
    }
    ask_twice = {
        "action": "ask_user",
        "question": "核心流程先覆盖哪个入口？",
        "options": [
            {"label": "移动端", "value": "先覆盖移动端入口"},
            {"label": "桌面端", "value": "先覆盖桌面端入口"},
        ],
    }
    script = {"ceo": [ask_once, ask_twice, HANDOFF], "cto": [{"action": "done", "remark": "完成"}]}
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["roles"], "推进首期版本")

    # 模拟隔了两小时才回来：创建时间已远超标称时限，但最近一次进展仍发生在刚才。
    async with SessionLocal() as session:
        run = await session.get(CollabRun, run_id)
        assert run is not None
        run.created_at = utcnow() - timedelta(hours=2)
        session.add(run)
        await session.commit()

    first = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "先做核心流程"})
    assert first.status_code == 200
    first_events = await read_sse(first)
    first_status = [e["status"] for e in first_events if e["type"] == "collab.run.status"]
    assert first_status and first_status[-1] == "waiting_user"
    assert not any("运行时限" in str(e.get("reason") or e.get("text") or "") for e in first_events)

    second = await client.post(f"/api/collab/runs/{run_id}/messages", json={"content": "先覆盖移动端入口"})
    assert second.status_code == 200
    second_events = await read_sse(second)
    second_status = [e["status"] for e in second_events if e["type"] == "collab.run.status"]
    assert second_status and second_status[-1] == "done"

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert [s["role_key"] for s in final["steps"]][:5] == ["user", "ceo", "user", "ceo", "user"]


async def test_done_run_can_be_reopened_by_user_follow_up(client: httpx.AsyncClient, tmp_path: Path):
    """运行完成后，用户继续找入口角色时应重新打开根工单，而不是只落一条无人响应的消息。"""
    script = {
        "ceo": [HANDOFF, {"action": "done", "remark": "已按补充需求确认"}],
        "cto": [{"action": "done", "remark": "完成"}],
    }
    scenario = await make_scenario(script, tmp_path)
    run_id, _ = await start_run(client, scenario["roles"], "先把登录页改成暗色主题")

    first = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert first["status"] == "done" and first["tickets"][0]["state"] == "done"

    response = await client.post(
        f"/api/collab/runs/{run_id}/messages", json={"content": "再加一个导出功能"}
    )
    assert response.status_code == 200
    await read_sse(response)

    final = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert final["status"] == "done"
    assert final["tickets"][0]["state"] == "done"
    assert final["steps"][-2]["role_key"] == "user"
    assert final["steps"][-2]["content"] == "再加一个导出功能"
    assert final["steps"][-1]["role_key"] == "ceo"


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


async def test_recover_interrupted_also_settles_streaming_steps(client: httpx.AsyncClient, full):
    """服务重启后残留的 streaming 步骤必须收尾。

    否则界面会一直显示「处理中」，点开该岗位却看不到任何输出（它永远不会再产出）。
    """
    run_id, _ = await start_run(client, full["roles"], "把登录页改成暗色主题")

    async with SessionLocal() as session:
        run = await session.get(CollabRun, run_id)
        run.status = "running"
        session.add(run)
        session.add(
            CollabStep(run_id=run_id, role_key="se", duty="engineer", sequence=99, status="streaming")
        )
        await session.commit()

        await recover_interrupted(session)

        refreshed = await session.get(CollabRun, run_id)
        assert refreshed.status == "interrupted"
        lingering = (
            await session.execute(select(CollabStep).where(CollabStep.status == "streaming"))
        ).scalars().all()
        assert lingering == []
        step = (
            await session.execute(select(CollabStep).where(CollabStep.sequence == 99))
        ).scalars().one()
        assert step.status == "interrupted"
        assert step.finished_at is not None


async def test_client_disconnect_does_not_stop_the_run(client: httpx.AsyncClient, full):
    """客户端断开（手机锁屏、切后台、网络抖动）不该打断推进。

    旧实现把推进绑在 HTTP 请求上：连接一断，正在跑的那一步就被取消、运行停在 ``running``，
    界面显示「处理中」却再也不动。现在推进跑在独立的后台任务里，断开只退订。
    """
    created = await client.post("/api/collab/teams", json=team_body(full["roles"]))
    assert created.status_code == 201, created.text
    run_id = (await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})).json()["id"]

    received = 0
    async with client.stream(
        "POST", f"/api/collab/runs/{run_id}/messages", json={"content": "把登录页改成暗色主题"}
    ) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                received += 1
                if received >= 2:  # 模拟客户端中途断开
                    break
    assert received >= 2

    detail = None
    for _ in range(400):
        detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
        if detail["status"] != "running":
            break
        await asyncio.sleep(0.05)

    assert detail is not None
    assert detail["status"] in {"waiting_user", "done", "error"}, detail["status"]
    # 断开之后仍然推进了多步：证明它没有跟着连接一起死掉
    assert detail["steps_used"] > 0
    assert not any(step["status"] == "streaming" for step in detail["steps"])


async def _wait_until(predicate, *, timeout: float = 10.0) -> bool:
    """轮询直到条件成立或超时（避免用固定 sleep 引入竞态）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return predicate()
        await asyncio.sleep(0.01)


def _stream_events(run_id: str) -> list[dict]:
    """总线里本轮的缓冲事件（测试用的内部视角，见 `app/collab/bus.py`）。"""
    stream = bus._streams.get(run_id)
    return list(stream.events) if stream else []


def _has_subscriber(run_id: str) -> bool:
    """这条运行的实时事件是否已经有订阅者挂上（同样是内部视角）。"""
    stream = bus._streams.get(run_id)
    return bool(stream and stream.subscribers)


async def test_stream_endpoint_replays_buffered_events_after_disconnect(client: httpx.AsyncClient, full):
    """断开后用 ``GET /runs/{id}/stream`` 重连：先补发本轮已产生的事件，再接着看实时增量。

    用闸门把 CEO 那一步稳稳卡在运行中，避免「刚好跑完」的竞态。推进请求放在后台任务里，
    取消它才是真断开 —— ``ASGITransport`` 会等应用跑完才返回响应，前台 ``break`` 测不出中途断开。
    """
    gate = full["state_dir"] / "gate-ceo"
    gate.write_text("wait", encoding="utf-8")

    created = await client.post("/api/collab/teams", json=team_body(full["roles"]))
    assert created.status_code == 201, created.text
    run_id = (await client.post("/api/collab/runs", json={"team_id": created.json()["id"]})).json()["id"]

    async def drive() -> None:
        async with client.stream(
            "POST", f"/api/collab/runs/{run_id}/messages", json={"content": "把登录页改成暗色主题"}
        ) as response:
            assert response.status_code == 200
            async for _ in response.aiter_lines():
                pass

    try:
        drive_task = asyncio.create_task(drive())
        assert await _wait_until(lambda: len(_stream_events(run_id)) >= 2), "推进没有开始"

        # 客户端断开（锁屏 / 切后台）：取消这条请求，运行本身不该受影响
        drive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drive_task
        assert (await client.get(f"/api/collab/runs/{run_id}")).json()["status"] == "running"

        # 断线重连：订阅接口应当先补发此刻缓冲区里的事件，再接着推实时增量。
        # 注意 `ASGITransport` 会把响应读完才交回客户端，所以订阅也放在后台任务里，
        # 才能在前台「等它挂上 → 放行闸门」，观察真实的重连时序。
        buffered = [event["type"] for event in _stream_events(run_id)]
        assert buffered[0] == "collab.run.started", buffered
        assert "collab.step.started" in buffered, buffered

        received: list[dict] = []

        async def subscribe() -> None:
            async with client.stream("GET", f"/api/collab/runs/{run_id}/stream") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        received.append(json.loads(line[6:]))

        subscribe_task = asyncio.create_task(subscribe())
        assert await _wait_until(lambda: _has_subscriber(run_id)), "订阅没有挂上"
        gate.unlink(missing_ok=True)  # 放行 CEO，后续步骤作为实时增量送到同一订阅
        await asyncio.wait_for(subscribe_task, timeout=20)
    finally:
        gate.unlink(missing_ok=True)

    kinds = [event["type"] for event in received]
    # 补发：重连后先看到的就是断开前已经发生的事件，顺序一致
    assert kinds[: len(buffered)] == buffered, kinds
    # 续看：同一订阅上继续收完这一轮，直到传输层收尾
    assert kinds.count("collab.step.finished") == 7, kinds
    assert kinds[-1] == "stream.closed", kinds
    assert received[0]["type"] == "collab.run.started"

    detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
    assert detail["status"] == "done", detail
    assert detail["steps_used"] == 7


async def test_delegate_step_records_target_for_handoff_chain(client: httpx.AsyncClient, full):
    """派发步骤要记住「把活交给了谁」。

    岗位会话靠这个字段还原交接链（CTO 把活交给码农，码农的抽屉里就能看到 CTO 的那句话）。
    """
    run_id, _ = await start_run(client, full["roles"], "把登录页改成暗色主题")
    for _ in range(400):
        detail = (await client.get(f"/api/collab/runs/{run_id}")).json()
        if detail["status"] != "running":
            break
        await asyncio.sleep(0.05)

    delegates = [step for step in detail["steps"] if step["action"] == "delegate"]
    assert delegates, "这次协作里应当有派发步骤"
    known_keys = {"ceo", "cto", "se", "qa"}
    for step in delegates:
        assert step["target_key"] in known_keys, f"派发目标应当是节点 key：{step['target_key']}"
    # 至少有一条「上游交给下游」的记录（ceo→cto 或 cto→se）
    assert any(step["target_key"] != step["role_key"] for step in delegates)
