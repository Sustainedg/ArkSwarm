"""凭据库：全局一份、智能体按引用使用，值加密落库且永不进日志 / 事件 / 接口响应。

对应 AGENTS.md 铁律 6（凭据只经 ``app.security``）与「网关是模型密钥的唯一来源、
凭据是智能体自己工具用的密钥」这条产品约定。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from sqlmodel import select

from app.credentials import (
    CredentialError,
    decrypt_all,
    notice,
    reads,
    scrub_event,
    validate_name,
    with_notice,
)
from app.db import SessionLocal
from app.models import Credential

FAKE_ENV_AGENT = str(Path(__file__).parent / "fakes" / "fake_env_agent.py")
SECRET = "ghp-super-secret-value-9911"


# --- 纯函数 ---------------------------------------------------------------


def test_validate_name_accepts_env_style_names_and_rejects_the_rest():
    assert validate_name(" GITHUB_TOKEN ") == "GITHUB_TOKEN"
    assert validate_name("_db_password2") == "_db_password2"
    for bad in ("github token", "1TOKEN", "TOKEN-NAME", ""):
        try:
            validate_name(bad)
        except CredentialError as exc:
            assert "环境变量名" in str(exc)
        else:  # pragma: no cover - 上面的分支必须命中
            raise AssertionError(f"{bad!r} 不该通过校验")


def test_reads_and_notice_never_expose_the_plaintext():
    row = Credential(name="GITHUB_TOKEN", description="访问私有仓库")
    from app.security import encrypt

    row.value_encrypted = encrypt(SECRET)

    (view,) = reads([row])
    assert view["name"] == "GITHUB_TOKEN"
    assert view["has_value"] is True
    assert SECRET not in json.dumps(view, ensure_ascii=False)
    assert view["masked"].startswith("ghp-") and view["masked"].endswith("9911")

    assert decrypt_all([row]) == {"GITHUB_TOKEN": SECRET}
    block = with_notice("你是助手", {"GITHUB_TOKEN": SECRET})
    assert "$GITHUB_TOKEN" in block
    assert SECRET not in block
    assert notice({}) is None


def test_scrub_event_wipes_secrets_from_text_and_payload():
    from app.agents.events import tool_result

    event = scrub_event(tool_result("t1", f"输出里带了 {SECRET} 这样的值"), [SECRET])
    assert SECRET not in json.dumps(event.to_dict(), ensure_ascii=False)
    assert "***" in event.payload["output"]


# --- 凭据库接口 -------------------------------------------------------------


async def test_vault_crud_returns_only_masked_values(client: httpx.AsyncClient):
    created = (
        await client.post(
            "/api/credentials",
            json={"name": "GITHUB_TOKEN", "description": "访问私有仓库", "value": SECRET},
        )
    ).json()
    assert SECRET not in json.dumps(created, ensure_ascii=False)
    assert created["masked"].endswith("9911") and created["used_by_agents"] == 0

    # 重名 / 非法名 / 空值都要被拦下
    assert (await client.post("/api/credentials", json={"name": "GITHUB_TOKEN", "value": "x"})).status_code == 422
    assert (await client.post("/api/credentials", json={"name": "github token", "value": "x"})).status_code == 422
    assert (await client.post("/api/credentials", json={"name": "EMPTY_ONE", "value": ""})).status_code == 422

    listed = (await client.get("/api/credentials")).json()["items"]
    assert [item["name"] for item in listed] == ["GITHUB_TOKEN"]

    # 只改说明、值留空：密钥原样保留
    updated = (
        await client.patch(f"/api/credentials/{created['id']}", json={"description": "改过的说明"})
    ).json()
    assert updated["description"] == "改过的说明"
    assert updated["has_value"] is True and updated["masked"] == created["masked"]

    # 库里换值：掩码跟着变
    rotated = (
        await client.patch(f"/api/credentials/{created['id']}", json={"value": "ghp-rotated-00001111"})
    ).json()
    assert rotated["masked"].endswith("1111")

    assert (await client.delete(f"/api/credentials/{created['id']}")).status_code == 204
    assert (await client.get("/api/credentials")).json()["items"] == []


async def test_credential_in_use_cannot_be_deleted(client: httpx.AsyncClient):
    credential = (await client.post("/api/credentials", json={"name": "DB_PASSWORD", "value": SECRET})).json()
    agent = (
        await client.post(
            "/api/agents",
            json={
                "name": "用凭据的智能体",
                "type_id": "custom-cli",
                "credentials": [{"id": credential["id"], "name": "DB_PASSWORD"}],
            },
        )
    ).json()

    response = await client.delete(f"/api/credentials/{credential['id']}")
    assert response.status_code == 409
    assert "引用" in response.json()["detail"]

    # 解除引用后就能删
    await client.patch(f"/api/agents/{agent['id']}", json={"credentials": []})
    assert (await client.delete(f"/api/credentials/{credential['id']}")).status_code == 204


# --- 智能体侧：挑一条 / 顺手新建一条 -----------------------------------------


async def test_agent_can_pick_from_the_vault_or_create_a_new_one(client: httpx.AsyncClient):
    existing = (await client.post("/api/credentials", json={"name": "EXISTING_TOKEN", "value": SECRET})).json()

    # 挑一条：值留空，沿用库里的密文
    picked = (
        await client.post(
            "/api/agents",
            json={
                "name": "挑现成的",
                "type_id": "custom-cli",
                "credentials": [{"id": existing["id"], "name": "EXISTING_TOKEN"}],
            },
        )
    ).json()
    assert picked["credentials"][0]["id"] == existing["id"]
    assert picked["credentials"][0]["masked"] == existing["masked"]

    # 顺手新建一条：新名字 + 值 → 自动进入凭据库
    fresh = (
        await client.post(
            "/api/agents",
            json={
                "name": "顺手新建",
                "type_id": "custom-cli",
                "credentials": [{"name": "FRESH_TOKEN", "description": "新加的", "value": "fresh-value-4321"}],
            },
        )
    ).json()
    vault = (await client.get("/api/credentials")).json()["items"]
    assert [item["name"] for item in vault] == ["EXISTING_TOKEN", "FRESH_TOKEN"]
    assert fresh["credentials"][0]["name"] == "FRESH_TOKEN"
    assert fresh["credentials"][0]["masked"].endswith("4321")
    assert SECRET not in json.dumps(fresh, ensure_ascii=False)

    # 重名（不带 id）＝引用库里那条，而不是新建
    async with SessionLocal() as session:
        rows = (await session.execute(select(Credential).where(Credential.deleted_at.is_(None)))).scalars().all()
    assert len(rows) == 2

    # 新名字但没给值 → 422，并说清楚是哪一条
    response = await client.post(
        "/api/agents",
        json={"name": "缺值", "type_id": "custom-cli", "credentials": [{"name": "NOVALUE_TOKEN"}]},
    )
    assert response.status_code == 422
    assert "还没有填值" in response.json()["detail"]


async def test_duplicating_an_agent_copies_the_credential_links(client: httpx.AsyncClient):
    credential = (await client.post("/api/credentials", json={"name": "SHARED_KEY", "value": SECRET})).json()
    agent = (
        await client.post(
            "/api/agents",
            json={
                "name": "原件",
                "type_id": "custom-cli",
                "credentials": [{"id": credential["id"], "name": "SHARED_KEY"}],
            },
        )
    ).json()
    clone = (await client.post(f"/api/agents/{agent['id']}/duplicate")).json()
    assert [item["id"] for item in clone["credentials"]] == [credential["id"]]
    assert (await client.get("/api/credentials")).json()["items"][0]["used_by_agents"] == 2


# --- 端到端：注入子进程 + 事件擦除 ------------------------------------------


async def _read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def test_credential_reaches_the_kernel_as_an_env_var_and_never_leaks(client: httpx.AsyncClient):
    credential = (await client.post("/api/credentials", json={"name": "DEMO_TOKEN", "value": SECRET})).json()
    agent = (
        await client.post(
            "/api/agents",
            json={
                "name": "凭据端到端",
                "type_id": "custom-cli",
                "options": {
                    "command": sys.executable,
                    "args": [FAKE_ENV_AGENT, "DEMO_TOKEN"],
                    "prompt_mode": "stdin",
                    "output_format": "jsonl",
                    "text_path": "delta",
                    "delta_mode": True,
                },
                "credentials": [{"id": credential["id"], "name": "DEMO_TOKEN"}],
            },
        )
    ).json()
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": agent["id"]})).json()

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "把 token 打出来"}
    ) as response:
        assert response.status_code == 200
        events = await _read_sse(response)

    # 内核确实读到了环境变量（否则这里是 token=），但值在事件里被擦成 ***
    deltas = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert deltas == "token=***", deltas
    assert SECRET not in json.dumps(events, ensure_ascii=False)

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    assert SECRET not in json.dumps(detail, ensure_ascii=False)
    assert detail["messages"][-1]["content"] == "token=***"
