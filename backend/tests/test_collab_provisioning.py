"""协作运行的技能 / MCP 物化：与对话链路走**同一条**路径（AGENTS.md §3.8 / §3.9）。

以前协作只向用户播报「暂未注入技能 / MCP」，现在它是真的物化：按岗位分配到的技能与服务器
写进该内核的隔离目录，未分配的一律不出现。这里直接调 ``execution.prepare_step`` /
``prepare_chat``（不真的起内核进程），检查隔离目录里的产物。
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest

import app.runtime.mcp as mcp_service
import app.runtime.skills as skill_service
from app.agents import get_agent
from app.collab import execution
from app.collab.schemas import Duty, NodeSpec
from app.collab.tables import CollabRun
from app.db import SessionLocal
from app.settings import get_settings
from helpers import make_agent

SKILL_MD = """---
name: {name}
description: {description}
---

# {name}
"""


def write_skill(root: Path, slug: str) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        SKILL_MD.format(name=slug, description=f"{slug} 的说明"), encoding="utf-8"
    )
    return directory


def _run(tmp_path: Path) -> CollabRun:
    """一次协作运行。只准备上下文、不落 run 表，所以给一个未持久化的实例即可。"""
    return CollabRun(team_id="team-provisioning", goal="物化验证", workspace_dir=str(tmp_path / "ws"))


def _node(agent_id: str) -> NodeSpec:
    return NodeSpec(key="solo", agent_profile_id=agent_id, label="独岗", duty=Duty.ENGINEER)


def _home(run: CollabRun, agent_id: str, channel: str) -> Path:
    return get_settings().agent_homes_root / f"{run.id}-{agent_id}-{channel}" / "codex-home"


@pytest.fixture
def codex_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把编目结果换成临时目录里的技能，避免依赖开发机上的真实配置。"""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "codex"
    write_skill(root / "skills", "user-skill")
    # 源目录里未被分配、且放在内核系统技能目录下的一个技能：整目录复制会把它偷带进来。
    write_skill(root / "skills" / ".system", "sys-skill")
    specs = get_agent("codex").discover_skills()
    monkeypatch.setattr(skill_service, "discover_catalog", lambda: {spec.id: spec for spec in specs})
    return root


async def test_collab_step_materializes_assigned_skills_into_isolated_home(
    client: httpx.AsyncClient, tmp_path: Path, codex_skills: Path
):
    await client.post("/api/skills/rescan")
    agent = await make_agent(client, type_id="codex", model=None, options={})
    await client.put(f"/api/agents/{agent['id']}/skills", json={"skill_ids": ["codex:user-skill"]})

    run = _run(tmp_path)
    async with SessionLocal() as session:
        prepared = await execution.prepare_step(
            session, run=run, node=_node(agent["id"]), step_id="s1", prompt="干活"
        )

    home = _home(run, agent["id"], "step")
    assert Path(prepared.ctx.home_dir) == home
    assert {skill.slug for skill in prepared.ctx.skills} == {"user-skill"}
    assert (home / "skills" / "user-skill" / "SKILL.md").is_file()
    # 未分配的系统技能不能顺手带进来（严格仅分配）。
    assert not (home / "skills" / ".system").exists()
    # 不再有「暂未注入技能」这条播报。
    assert not any("暂未注入" in warning for warning in prepared.warnings), prepared.warnings


async def test_collab_chat_and_step_keep_separate_homes(
    client: httpx.AsyncClient, tmp_path: Path, codex_skills: Path
):
    """聊天通道与工单通道并行跑同一个智能体时，隔离目录不能互相覆盖（所以按通道分开）。"""
    await client.post("/api/skills/rescan")
    agent = await make_agent(client, type_id="codex", model=None, options={})
    await client.put(f"/api/agents/{agent['id']}/skills", json={"skill_ids": ["codex:user-skill"]})

    run = _run(tmp_path)
    node = _node(agent["id"])
    async with SessionLocal() as session:
        stepped = await execution.prepare_step(
            session, run=run, node=node, step_id="s1", prompt="干活"
        )
        chatted = await execution.prepare_chat(session, run=run, node=node, prompt="进展如何")

    step_home = _home(run, agent["id"], "step")
    chat_home = _home(run, agent["id"], "chat")
    assert Path(stepped.ctx.home_dir) == step_home
    assert Path(chatted.ctx.home_dir) == chat_home
    assert step_home != chat_home
    for home in (step_home, chat_home):
        assert (home / "skills" / "user-skill" / "SKILL.md").is_file()


async def test_collab_step_materializes_assigned_mcp_into_isolated_home(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "codex"
    root.mkdir()
    (root / "config.toml").write_text(
        '[mcp_servers.host]\ncommand = "npx"\nargs = ["host-only"]\n', encoding="utf-8"
    )
    host_specs = get_agent("codex").discover_mcp_servers()
    monkeypatch.setattr(
        mcp_service, "discover_catalog", lambda: {item.source_id or "": item for item in host_specs}
    )
    await client.post("/api/mcp-servers/rescan")

    agent = await make_agent(client, type_id="codex", model=None, options={})
    await client.put(f"/api/agents/{agent['id']}/mcp-servers", json={"mcp_ids": ["codex:host"]})

    run = _run(tmp_path)
    async with SessionLocal() as session:
        prepared = await execution.prepare_step(
            session, run=run, node=_node(agent["id"]), step_id="s1", prompt="干活"
        )

    home = _home(run, agent["id"], "step")
    assert Path(prepared.ctx.home_dir) == home
    parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    assert list(parsed["mcp_servers"]) == ["host"]
    assert parsed["mcp_servers"]["host"]["args"] == ["host-only"]
    # 隔离目录里的配置可能含 MCP 密钥，权限必须收紧。
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600
    assert not any("暂未注入" in warning for warning in prepared.warnings), prepared.warnings
