"""技能市场（SkillHub）：列表解析、下载安装、安全解压与卸载。

网络部分一律用 ``httpx.MockTransport`` 造假，绝不真的联网；落盘落在 tmp 技能根里。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

import app.api.skillhub as skillhub_api
import app.runtime.skills as skill_service
from app import skillhub
from app.db import SessionLocal
from helpers import make_gateway, make_profile

SKILL_MD = """---
name: 市场技能
description: 来自技能市场的示例技能
version: "1.0.0"
---

# 市场技能

正文。
"""


def _zip(files: dict[str, str], *, top: str | None = "market-skill") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, body in files.items():
            zf.writestr(f"{top}/{name}" if top else name, body)
    return buffer.getvalue()


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/skills":
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "total": 1,
                    "skills": [
                        {
                            "slug": "market-skill",
                            "name": "市场技能",
                            "description": "来自技能市场的示例技能",
                            "description_zh": "来自技能市场的示例技能",
                            "version": "1.0.0",
                            "downloads": 123,
                            "stars": 4,
                            "score": 9.5,
                            "category": "dev-programming",
                            "ownerName": "tester",
                            "source": "community",
                            "verified": True,
                            "iconUrl": "https://example.com/i.png",
                        }
                    ],
                },
            },
        )
    if path == "/api/v1/skills/market-skill":
        return httpx.Response(
            200,
            json={
                "slug": "market-skill",
                "displayName": "市场技能",
                "description_zh": "来自技能市场的示例技能",
                "latestVersion": {"version": "1.0.0"},
                "labels": {"requires_api_key": "true"},
                "securityReports": {
                    "keen": {"status": "benign", "statusText": "安全，无风险"}
                },
            },
        )
    if path == "/api/v1/skills/missing":
        return httpx.Response(404, json={"error": "Skill not found"})
    if path == "/api/v1/download":
        return httpx.Response(
            200,
            content=_zip(
                {
                    "SKILL.md": SKILL_MD,
                    "references/note.md": "note\n",
                    "scripts/run.sh": "echo hi\n",
                }
            ),
            headers={"content-type": "application/zip"},
        )
    return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch):
    """把市场客户端的三个入口都接到 MockTransport 上。"""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url=skillhub.DEFAULT_API_BASE
    )

    async def fake_list(**kwargs):
        return await skillhub.list_skills(client=client, **kwargs)

    async def fake_detail(slug, **kwargs):
        return await skillhub.get_skill(slug, client=client, **kwargs)

    async def fake_download(slug, *, version=None, **kwargs):
        return await skillhub.download_skill(slug, version=version, client=client, **kwargs)

    monkeypatch.setattr(skillhub_api, "fetch_skill_list", fake_list)
    monkeypatch.setattr(skillhub_api, "fetch_skill_detail", fake_detail)
    monkeypatch.setattr(skillhub_api, "fetch_skill_archive", fake_download)
    yield client


@pytest.fixture
def skills_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """给 claude-code 一个临时技能根，避免写到真实的 ~/.claude。

    同时把编目也指向这个临时根（与真实适配器一致：每次重扫磁盘），
    这样「安装落盘 → 重扫收录」这条链路能被真实地走通。
    """
    from app.agents.skills import MARKET_SOURCE, SkillRoot, scan_skill_roots

    root = tmp_path / "claude-skills"
    roots = [
        SkillRoot(str(root / ".skillhub"), source_kind=MARKET_SOURCE),
        SkillRoot(str(root), source_kind="user"),
    ]

    def fake_catalog() -> dict:
        specs = scan_skill_roots(roots, type_id="claude-code")
        return {spec.id: spec for spec in specs}

    monkeypatch.setattr(skill_service, "skill_roots_for_type", lambda type_id: roots)
    monkeypatch.setattr(skill_service, "install_root_for_type", lambda type_id: roots[1])
    monkeypatch.setattr(
        skill_service, "market_install_root", lambda type_id: str(root / ".skillhub")
    )
    monkeypatch.setattr(skill_service, "discover_catalog", fake_catalog)
    return root


async def test_market_list_parsing(market):
    result = await skillhub.list_skills(client=market, page=1, page_size=10)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["slug"] == "market-skill"
    assert item["downloads"] == 123
    assert item["verified"] is True
    assert item["url"].endswith("/skills/market-skill")


async def test_market_list_endpoint(client, market):
    response = await client.get("/api/skillhub/skills", params={"sort_by": "downloads"})
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["slug"] == "market-skill"


async def test_kernels_endpoint_lists_skill_capable_kernels(client):
    items = (await client.get("/api/skillhub/kernels")).json()["items"]
    ids = {item["type_id"] for item in items}
    assert {"claude-code", "codex"} <= ids


async def test_install_then_appears_and_uninstall(client, market, skills_home: Path):
    response = await client.post(
        "/api/skillhub/install", json={"slug": "market-skill", "type_id": "claude-code"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skill_id"] == "claude-code:market-skill"
    assert body["requires_api_key"] is True

    # 落盘位置正确，且带上了市场安装标记
    installed = skills_home / ".skillhub" / "market-skill"
    assert (installed / "SKILL.md").is_file()
    assert (installed / ".skillhub.json").is_file()

    # 已进入统一编目，来源是市场技能
    items = (await client.get("/api/skills", params={"source_kind": "market"})).json()["items"]
    assert [item["slug"] for item in items] == ["market-skill"]
    assert items[0]["source_label"] == "腾讯 SkillHub"
    assert items[0]["is_market"] is True

    # 卸载：目录消失、编目里也不再保留这条技能
    removed = await client.post(
        "/api/skillhub/uninstall", json={"skill_id": "claude-code:market-skill"}
    )
    assert removed.status_code == 200, removed.text
    assert not installed.exists()
    assert (await client.get("/api/skills/claude-code:market-skill")).status_code == 404
    left = (await client.get("/api/skills", params={"source_kind": "market"})).json()["items"]
    assert left == []


async def test_install_unknown_kernel_rejected(client, market):
    response = await client.post(
        "/api/skillhub/install", json={"slug": "market-skill", "type_id": "不存在的内核"}
    )
    assert response.status_code == 422
    assert "未知" in response.json()["detail"] or "不支持" in response.json()["detail"]


async def test_installed_market_skill_can_be_assigned(client, market, skills_home: Path):
    """装进来的技能走的是既有链路：安装 → 分配 → 被运行层取到。"""
    installed = await client.post(
        "/api/skillhub/install", json={"slug": "market-skill", "type_id": "claude-code"}
    )
    assert installed.status_code == 200, installed.text

    gateway = await make_gateway(client)
    agent = await make_profile(
        client, name="Claude 智能体", type_id="claude-code", gateway_id=gateway["id"], model=None
    )
    assigned = await client.put(
        f"/api/agents/{agent['id']}/skills", json={"skill_ids": ["claude-code:market-skill"]}
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["skill_ids"] == ["claude-code:market-skill"]

    # 运行层（skills_for_agent）能按分配取到这个市场技能
    from sqlmodel import select

    from app.models import AgentProfile

    session = SessionLocal()
    try:
        profile = (
            await session.execute(select(AgentProfile).where(AgentProfile.id == agent["id"]))
        ).scalars().one()
        specs = await skill_service.skills_for_agent(session, profile)
    finally:
        await session.close()
    assert [spec.slug for spec in specs] == ["market-skill"]


def test_extract_rejects_zip_slip(tmp_path: Path):
    bad = _zip({"../evil.txt": "x", "SKILL.md": "hi"}, top=None)
    with pytest.raises(skillhub.SkillHubError):
        skillhub.extract_skill_archive(bad, tmp_path / "dest")
    assert not (tmp_path / "evil.txt").exists()


def test_extract_rejects_absolute_paths(tmp_path: Path):
    bad = _zip({"/etc/passwd": "x", "SKILL.md": "hi"}, top=None)
    with pytest.raises(skillhub.SkillHubError):
        skillhub.extract_skill_archive(bad, tmp_path / "dest")


def test_extract_flattens_single_top_dir(tmp_path: Path):
    dest = tmp_path / "dest"
    count = skillhub.extract_skill_archive(_zip({"SKILL.md": "hi", "a/b.txt": "x"}), dest)
    assert count == 2
    assert (dest / "SKILL.md").is_file()
    assert (dest / "a" / "b.txt").is_file()


def test_extract_rejects_empty(tmp_path: Path):
    with pytest.raises(skillhub.SkillHubError):
        skillhub.extract_skill_archive(_zip({}, top=None), tmp_path / "dest")


async def test_uninstall_refuses_non_market_skill(client, skills_home: Path, monkeypatch):
    """内核自带（非 market）技能不能被市场卸载接口删掉。"""
    from app.models import Skill

    async def fake_get_row(session, skill_id):
        return Skill(
            id="claude-code:doctor",
            type_id="claude-code",
            slug="doctor",
            name="doctor",
            source_kind="plugin",
            path=str(skills_home / "doctor"),
        )

    monkeypatch.setattr(skill_service, "get_row", fake_get_row)
    response = await client.post("/api/skillhub/uninstall", json={"skill_id": "claude-code:doctor"})
    assert response.status_code == 422
    assert "不是市场技能" in response.json()["detail"]
