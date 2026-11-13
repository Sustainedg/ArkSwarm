"""协作组接口：CRUD、校验端点和「有运行记录不能删」的 409 不变量。"""
from __future__ import annotations

import httpx
import pytest_asyncio

from app.collab import graph as g
from app.collab.schemas import Duty, EdgeKind, EdgeSpec, GraphSpec, NodeSpec
from app.collab.tables import CollabRun, CollabTicket
from app.db import SessionLocal
from app.models import AgentProfile, Gateway


@pytest_asyncio.fixture
async def roles() -> dict[str, str]:
    """直接落库几个智能体，避免把协作用例绑死在「智能体」接口上。"""
    async with SessionLocal() as session:
        gateway = Gateway(name="测试网关", kind="custom", base_url="https://gw.example/v1")
        session.add(gateway)
        await session.commit()
        await session.refresh(gateway)
        created = {}
        for key, name in (("ceo", "CEO"), ("cto", "CTO"), ("se", "SE"), ("qa", "审查员")):
            role = AgentProfile(
                name=name,
                type_id="custom-cli",
                gateway_id=gateway.id,
                options={},
                system_prompt="",
            )
            session.add(role)
            await session.commit()
            await session.refresh(role)
            created[key] = role.id
        return created


def graph_body(roles: dict[str, str]) -> dict:
    spec = GraphSpec(
        nodes=[
            NodeSpec(key="ceo", agent_profile_id=roles["ceo"], label="CEO", duty=Duty.COORDINATOR),
            NodeSpec(key="se", agent_profile_id=roles["se"], label="SE", duty=Duty.ENGINEER),
            NodeSpec(key="qa", agent_profile_id=roles["qa"], label="审查员", duty=Duty.REVIEWER),
        ],
        edges=[
            EdgeSpec(from_key="ceo", to_key="se", kind=EdgeKind.ASSIGN),
            EdgeSpec(from_key="se", to_key="qa", kind=EdgeKind.REVIEW),
            EdgeSpec(from_key="qa", to_key="se", kind=EdgeKind.REWORK),
        ],
    )
    return g.to_storable(spec)


async def test_team_crud_and_validation(client: httpx.AsyncClient, roles):
    created = await client.post(
        "/api/collab/teams",
        json={"name": "客户端研发组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    assert created.status_code == 201, created.text
    team = created.json()
    assert team["node_count"] == 3
    assert team["validation"]["ok"] is True, team["validation"]
    assert team["budget"]["max_rounds_per_ticket"] == 3  # 未指定时用默认预算
    assert team["id"]

    listed = await client.get("/api/collab/teams")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["items"]] == ["客户端研发组"]

    detail = await client.get(f"/api/collab/teams/{team['id']}")
    assert detail.status_code == 200
    assert detail.json()["entry_key"] == "ceo"

    # 图不合法也能存草稿（编辑器要能边画边存），但要把问题报出来
    broken = graph_body(roles)
    broken["edges"] = [e for e in broken["edges"] if e["kind"] != "rework"]
    saved = await client.put(f"/api/collab/teams/{team['id']}", json={"graph": broken})
    assert saved.status_code == 200, saved.text
    codes = {issue["code"] for issue in saved.json()["validation"]["errors"]}
    assert "edge.review_without_rework" in codes

    # 只校验不保存：传一份合法图应恢复 ok，且不影响库里那份
    checked = await client.post(
        f"/api/collab/teams/{team['id']}/validate",
        json={"entry_key": "ceo", "graph": graph_body(roles)},
    )
    assert checked.status_code == 200 and checked.json()["ok"] is True
    still_broken = await client.get(f"/api/collab/teams/{team['id']}")
    assert still_broken.json()["validation"]["ok"] is False

    copied = await client.post(f"/api/collab/teams/{team['id']}/duplicate")
    assert copied.status_code == 201
    assert copied.json()["name"] == "客户端研发组（副本）"

    deleted = await client.delete(f"/api/collab/teams/{copied.json()['id']}")
    assert deleted.status_code == 204


async def test_unknown_team_is_404(client: httpx.AsyncClient):
    assert (await client.get("/api/collab/teams/nope")).status_code == 404
    assert (await client.put("/api/collab/teams/nope", json={"name": "x"})).status_code == 404
    assert (await client.delete("/api/collab/teams/nope")).status_code == 404
    assert (await client.post("/api/collab/teams/nope/duplicate")).status_code == 404


async def test_structurally_invalid_graph_is_422(client: httpx.AsyncClient):
    response = await client.post(
        "/api/collab/teams",
        json={"name": "坏图", "graph": {"nodes": [{"key": "ceo", "agent_profile_id": "r1", "duty": "老板"}]}},
    )
    assert response.status_code == 422


async def test_team_with_runs_cannot_be_deleted(client: httpx.AsyncClient, roles):
    created = await client.post("/api/collab/teams", json={"name": "有运行的组"})
    team_id = created.json()["id"]
    async with SessionLocal() as session:
        session.add(CollabRun(team_id=team_id, title="跑过一次", goal="目标"))
        await session.commit()

    response = await client.delete(f"/api/collab/teams/{team_id}")
    assert response.status_code == 409
    assert "运行记录" in response.json()["detail"]


async def test_renaming_a_key_referenced_by_a_run_is_409(client: httpx.AsyncClient, roles):
    """改节点 key 会打断已经在跑的协作：历史工单里记的是旧 key。

    真实事故：画布上把 `dev` 改名成 `SDE` 之后，持有旧 key 的工单再也找不到岗位。
    改名与「删掉旧节点、新建一个」在图里无从区分，只能在这里拦下来。
    """
    created = await client.post(
        "/api/collab/teams",
        json={"name": "会改名的组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    team_id = created.json()["id"]
    async with SessionLocal() as session:
        run = CollabRun(team_id=team_id, title="跑过一次", goal="目标")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        session.add(
            CollabTicket(
                run_id=run.id,
                to_agent_profile_id=roles["se"],
                owner_key="ceo",
                holder_key="se",
                title="干活",
            )
        )
        await session.commit()

    renamed = graph_body(roles)
    renamed["nodes"][1]["key"] = "sde"  # se → sde
    response = await client.put(f"/api/collab/teams/{team_id}", json={"graph": renamed})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "se" in detail and "承担者" in detail and "删除这些运行记录" in detail

    stored = await client.get(f"/api/collab/teams/{team_id}")
    assert {node["key"] for node in stored.json()["graph"]["nodes"]} == {"ceo", "se", "qa"}


async def test_renaming_an_unreferenced_key_is_saved(client: httpx.AsyncClient, roles):
    """只拦真正被引用到的 key：运行里没出现过的节点，改名照旧能存。"""
    created = await client.post(
        "/api/collab/teams",
        json={"name": "能改名的组", "entry_key": "ceo", "graph": graph_body(roles)},
    )
    team_id = created.json()["id"]
    async with SessionLocal() as session:
        session.add(CollabRun(team_id=team_id, title="跑过一次", goal="目标"))
        await session.commit()

    renamed = graph_body(roles)
    renamed["nodes"][1]["key"] = "sde"
    response = await client.put(f"/api/collab/teams/{team_id}", json={"graph": renamed})
    assert response.status_code == 200, response.text
    assert "sde" in {node["key"] for node in response.json()["graph"]["nodes"]}


async def test_deleted_role_is_reported_as_validation_error(client: httpx.AsyncClient, roles):
    body = {"name": "智能体被删的组", "entry_key": "ceo", "graph": graph_body(roles)}
    body["graph"]["nodes"][1]["agent_profile_id"] = "r-deleted"
    created = await client.post("/api/collab/teams", json=body)
    codes = {issue["code"] for issue in created.json()["validation"]["errors"]}
    assert "node.agent_missing" in codes
