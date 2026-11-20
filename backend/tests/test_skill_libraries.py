"""用户技能库：自带技能共享，导入 / 市场安装的技能按用户隔离。

与 ``test_skills.py`` 的分工：那边验证编目、导入导出与物化；这里在多用户形态下验证
「甲导入的技能乙看不到、分配不了、删不掉」，以及管理员能看到全量目录。
"""
from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import app.api.skillhub as skillhub_api
import app.runtime.skills as skill_service
from app.agents.skills import SkillRoot, parse_skill_dir
from app.db import engine, init_db
from app.main import app
from app.models import SQLModel
from app.settings import get_settings

SKILL_MD = """---
name: {name}
description: {description}
version: "1.0.0"
---

# {name}

正文。
"""


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _register(c: httpx.AsyncClient, username: str) -> dict:
    resp = await c.post(
        "/api/auth/register", json={"username": username, "password": "password123"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _members(c: httpx.AsyncClient) -> tuple[dict, dict]:
    """首任注册者是管理员，再建两个普通用户。"""
    await _register(c, "root")
    return await _register(c, "alice"), await _register(c, "bob")


def _zip(slug: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(
            f"{slug}/SKILL.md",
            SKILL_MD.format(name=slug, description=f"{slug} 的说明"),
        )
        zf.writestr(f"{slug}/scripts/run.sh", "echo hi\n")
    return buffer.getvalue()


async def _import(c: httpx.AsyncClient, token: str, slug: str = "team-skill") -> dict:
    resp = await c.post(
        "/api/skills/import",
        json={
            "type_id": "claude-code",
            "name": f"{slug}.zip",
            "content_base64": base64.b64encode(_zip(slug)).decode(),
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _create_agent(c: httpx.AsyncClient, token: str, name: str) -> dict:
    resp = await c.post(
        "/api/agents",
        json={"name": name, "type_id": "claude-code"},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def shared_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """造一个「内核自带」的共享技能，并让适配器把它声明为 system 根。"""
    root = tmp_path / "shared-skills"
    directory = root / "shared-builtin"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        SKILL_MD.format(name="共享技能", description="内核自带技能"), encoding="utf-8"
    )
    spec = parse_skill_dir(directory, type_id="claude-code", source_kind="system")
    assert spec is not None
    monkeypatch.setattr(
        skill_service, "discover_catalog", lambda: {spec.id: spec}
    )
    monkeypatch.setattr(
        skill_service,
        "skill_roots_for_type",
        lambda _type_id: [SkillRoot(str(root), source_kind="system")],
    )
    return directory


async def test_users_only_see_their_own_library_and_shared_skills(
    client: httpx.AsyncClient, shared_skill: Path
):
    alice, bob = await _members(client)
    alice_skill = await _import(client, alice["access_token"])
    bob_skill = await _import(client, bob["access_token"])

    # 同一个 slug 在不同用户的库里互不冲突，标识里带归属。
    assert alice_skill["skill_id"] == f"claude-code:lib:{alice['user']['id']}:team-skill"
    assert bob_skill["skill_id"] == f"claude-code:lib:{bob['user']['id']}:team-skill"

    alice_items = (
        await client.get("/api/skills", headers=_auth(alice["access_token"]))
    ).json()["items"]
    bob_items = (
        await client.get("/api/skills", headers=_auth(bob["access_token"]))
    ).json()["items"]

    assert {item["slug"] for item in alice_items} == {"shared-builtin", "team-skill"}
    assert {item["slug"] for item in bob_items} == {"shared-builtin", "team-skill"}
    alice_mine = next(item for item in alice_items if item["slug"] == "team-skill")
    assert alice_mine["is_own"] is True
    assert alice_mine["owner_name"] == "alice"
    shared = next(item for item in alice_items if item["slug"] == "shared-builtin")
    assert shared["is_own"] is False
    assert shared["deletable"] is False  # 内核自带技能只读

    # 管理员看到全部，并带归属用户名。
    root = await client.post(
        "/api/auth/login",
        json={"username": "root", "password": "password123"},
    )
    admin_items = (
        await client.get("/api/skills", headers=_auth(root.json()["access_token"]))
    ).json()["items"]
    team = [item for item in admin_items if item["slug"] == "team-skill"]
    assert {item["owner_name"] for item in team} == {"alice", "bob"}


async def test_other_users_skill_is_invisible_and_cannot_be_assigned(
    client: httpx.AsyncClient, shared_skill: Path
):
    alice, bob = await _members(client)
    alice_skill = await _import(client, alice["access_token"])
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")

    # 详情：看不到就是 404（不泄露存在性）。
    gone = await client.get(
        f"/api/skills/{alice_skill['skill_id']}", headers=_auth(bob["access_token"])
    )
    assert gone.status_code == 404

    # 分配别人的技能：服务端拒绝。
    denied = await client.put(
        f"/api/agents/{bob_agent['id']}/skills",
        json={"skill_ids": [alice_skill["skill_id"]]},
        headers=_auth(bob["access_token"]),
    )
    assert denied.status_code == 422
    assert "技能库" in denied.json()["detail"]

    # 共享技能可以正常分配。
    allowed = await client.put(
        f"/api/agents/{bob_agent['id']}/skills",
        json={"skill_ids": ["claude-code:shared-builtin"]},
        headers=_auth(bob["access_token"]),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["skill_ids"] == ["claude-code:shared-builtin"]


async def test_delete_only_own_skill(client: httpx.AsyncClient, shared_skill: Path):
    alice, bob = await _members(client)
    alice_skill = await _import(client, alice["access_token"])
    await _import(client, bob["access_token"])

    denied = await client.delete(
        f"/api/skills/{alice_skill['skill_id']}", headers=_auth(bob["access_token"])
    )
    assert denied.status_code == 404

    removed = await client.delete(
        f"/api/skills/{alice_skill['skill_id']}", headers=_auth(alice["access_token"])
    )
    assert removed.status_code == 200, removed.text

    # 乙的同名技能还在，甲的已消失。
    bob_items = (
        await client.get("/api/skills", headers=_auth(bob["access_token"]))
    ).json()["items"]
    assert any(item["slug"] == "team-skill" for item in bob_items)


async def test_market_install_is_per_user(
    client: httpx.AsyncClient, shared_skill: Path, monkeypatch: pytest.MonkeyPatch
):
    """市场安装落到各自的技能库：同名安装互不覆盖，卸载也互不影响。"""

    async def fake_detail(slug: str, **kwargs) -> dict:
        return {"requires_api_key": False, "version": "1.0.0"}

    async def fake_download(slug: str, *, version: str | None = None, **kwargs) -> bytes:
        return _zip("market-skill")

    monkeypatch.setattr(skillhub_api, "fetch_skill_detail", fake_detail)
    monkeypatch.setattr(skillhub_api, "fetch_skill_archive", fake_download)

    alice, bob = await _members(client)
    installed = {}
    for who in (alice, bob):
        resp = await client.post(
            "/api/skillhub/install",
            json={"slug": "market-skill", "type_id": "claude-code"},
            headers=_auth(who["access_token"]),
        )
        assert resp.status_code == 200, resp.text
        installed[who["user"]["id"]] = resp.json()["skill_id"]

    assert installed[alice["user"]["id"]] == (
        f"claude-code:lib:{alice['user']['id']}:market-skill"
    )
    assert installed[bob["user"]["id"]] == f"claude-code:lib:{bob['user']['id']}:market-skill"
    library = skill_service.get_settings().skill_libraries_root
    assert (library / alice["user"]["id"] / "market-skill" / "SKILL.md").is_file()
    assert (library / bob["user"]["id"] / "market-skill" / "SKILL.md").is_file()

    # 乙卸载不了甲的，甲的安装不受影响。
    denied = await client.post(
        "/api/skillhub/uninstall",
        json={"skill_id": installed[alice["user"]["id"]]},
        headers=_auth(bob["access_token"]),
    )
    assert denied.status_code == 404
    alice_items = (
        await client.get("/api/skills", headers=_auth(alice["access_token"]))
    ).json()["items"]
    assert any(item["slug"] == "market-skill" and item["is_own"] for item in alice_items)


async def test_duplicating_another_users_agent_drops_private_skills(
    client: httpx.AsyncClient, shared_skill: Path
):
    """管理员复制别人的智能体时，不能把对方技能库里的私有技能顺手带过去。"""
    _, bob = await _members(client)
    bob_skill = await _import(client, bob["access_token"])
    bob_agent = await _create_agent(client, bob["access_token"], "Bob 智能体")
    assigned = await client.put(
        f"/api/agents/{bob_agent['id']}/skills",
        json={"skill_ids": [bob_skill["skill_id"], "claude-code:shared-builtin"]},
        headers=_auth(bob["access_token"]),
    )
    assert assigned.status_code == 200, assigned.text

    root = await client.post(
        "/api/auth/login",
        json={"username": "root", "password": "password123"},
    )
    clone = await client.post(
        f"/api/agents/{bob_agent['id']}/duplicate",
        headers=_auth(root.json()["access_token"]),
    )
    assert clone.status_code == 201, clone.text
    # 共享技能保留；Bob 的私有技能不能出现在管理员的副本里。
    assert clone.json()["skill_ids"] == ["claude-code:shared-builtin"]
