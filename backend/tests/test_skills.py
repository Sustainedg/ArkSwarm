"""技能：解析、统一编目、角色分配与运行时物化。

技能的相关约定见 AGENTS.md §3（契约）与 §4（扩展手册）。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import app.runtime.skills as skill_service
from app.agents import AgentContext, get_agent
from app.agents.skills import (
    SkillMount,
    SkillSpec,
    parse_skill_dir,
    safe_skill_dirname,
    scan_skill_root,
)
from app.db import SessionLocal
from app.models import Conversation
from app.settings import get_settings

SKILL_MD = """---
name: {name}
description: {description}
{extra}---

# {name}

正文第一段。
"""


def write_skill(
    root: Path,
    slug: str,
    *,
    name: str | None = None,
    extra: str = "",
    body_extra: str = "",
) -> Path:
    directory = root / slug
    (directory / "scripts").mkdir(parents=True, exist_ok=True)
    (directory / "references").mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        SKILL_MD.format(name=name or slug, description=f"{slug} 的说明", extra=extra) + body_extra,
        encoding="utf-8",
    )
    (directory / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")
    (directory / "references" / "note.md").write_text("note\n", encoding="utf-8")
    return directory


def spec_for(
    directory: Path, *, type_id: str = "custom-cli", source_kind: str = "user"
) -> SkillSpec:
    spec = parse_skill_dir(directory, type_id=type_id, source_kind=source_kind)
    assert spec is not None
    return spec


def context(**overrides) -> AgentContext:
    data: dict = {"agent_id": "a1", "agent_name": "测试智能体", "prompt": "hi"}
    data.update(overrides)
    return AgentContext(**data)


# --- 解析 -------------------------------------------------------------------


def test_parse_skill_dir_reads_metadata_and_files(tmp_path: Path):
    directory = write_skill(
        tmp_path,
        "demo-skill",
        extra=(
            "version: 1.2.0\nauthor: 张三\nallowed-tools:\n  - Read\n  - Bash(git *)\n"
            "metadata:\n  short-description: 简短说明\n"
        ),
    )
    spec = spec_for(directory)

    assert spec.id == "custom-cli:demo-skill"
    assert spec.name == "demo-skill"
    assert spec.description == "demo-skill 的说明"
    assert spec.version == "1.2.0"
    assert spec.author == "张三"
    assert spec.allowed_tools == ["Read", "Bash(git *)"]
    assert {item.path for item in spec.files} == {
        "SKILL.md", "scripts/run.sh", "references/note.md",
    }
    assert spec.total_bytes > 0
    assert spec.fingerprint


def test_parse_skill_dir_falls_back_when_frontmatter_is_broken(tmp_path: Path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        '---\nname: "未闭合\ndescription: 坏掉的\n---\n\n# 标题\n\n这是正文第一段。\n',
        encoding="utf-8",
    )
    spec = spec_for(directory)

    # frontmatter 解析失败时退化为「目录名 + 正文第一段」，不能让整个编目崩掉。
    assert spec.name == "broken"
    assert spec.description == "这是正文第一段。"


def test_parse_skill_dir_without_skill_file_returns_none(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert parse_skill_dir(empty, type_id="custom-cli") is None


def test_scan_skill_root_ignores_symlinks_and_missing_root(tmp_path: Path):
    write_skill(tmp_path, "real")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:  # 某些文件系统不支持符号链接
        linked = None  # type: ignore[assignment]

    slugs = {spec.slug for spec in scan_skill_root(tmp_path, type_id="custom-cli")}
    assert "real" in slugs
    if linked is not None:
        assert "linked" not in slugs
    assert scan_skill_root(tmp_path / "不存在", type_id="custom-cli") == []


@pytest.mark.parametrize("slug", ["..", ".", "a/b", "", "有中文", "a\\b"])
def test_safe_skill_dirname_rejects_unsafe_names(slug: str):
    assert safe_skill_dirname(slug) is None


def test_safe_skill_dirname_accepts_plain_names():
    assert safe_skill_dirname("demo-skill.v2") == "demo-skill.v2"


# --- 物化 -------------------------------------------------------------------


def test_materialize_copies_skills_and_drops_stale_ones(tmp_path: Path):
    source = tmp_path / "source"
    write_skill(source, "alpha")
    write_skill(source, "beta")
    specs = [spec_for(source / "alpha"), spec_for(source / "beta")]

    home = tmp_path / "home"
    warnings = skill_service.materialize(home, SkillMount(home_name="claude-home"), specs)
    assert warnings == []
    assert (home / "skills" / "alpha" / "scripts" / "run.sh").is_file()
    assert (home / "skills" / "beta" / "SKILL.md").is_file()
    assert (home.stat().st_mode & 0o777) == 0o700

    # 取消分配后，技能必须从隔离目录里消失。
    warnings = skill_service.materialize(home, SkillMount(home_name="claude-home"), specs[:1])
    assert warnings == []
    assert (home / "skills" / "alpha").is_dir()
    assert not (home / "skills" / "beta").exists()


def test_materialize_carries_and_patches_kernel_settings(tmp_path: Path):
    source = tmp_path / "source"
    write_skill(source, "alpha")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-x"}, "enabledPlugins": {"keep@market": True}}
        ),
        encoding="utf-8",
    )
    mount = SkillMount(
        home_name="claude-home",
        carry=[(str(settings_file), "settings.json")],
        patch={
            "settings.json": {
                "enabledPlugins": {"builtin@market": False},
                "disableBundledSkills": True,
            }
        },
    )

    home = tmp_path / "home"
    assert skill_service.materialize(home, mount, [spec_for(source / "alpha")]) == []

    data = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-x"
    assert data["enabledPlugins"] == {"keep@market": True, "builtin@market": False}
    assert data["disableBundledSkills"] is True
    assert (home / "settings.json").stat().st_mode & 0o777 == 0o600


def test_materialize_skips_unsafe_and_missing_skills(tmp_path: Path):
    source = tmp_path / "source"
    write_skill(source, "alpha")
    good = spec_for(source / "alpha")
    broken = SkillSpec(
        type_id="custom-cli", slug="../逃逸", name="危险技能", path=str(source / "alpha")
    )
    gone = SkillSpec(type_id="custom-cli", slug="gone", name="已删除", path=str(source / "不存在"))

    home = tmp_path / "home"
    warnings = skill_service.materialize(
        home, SkillMount(home_name="claude-home"), [good, broken, gone]
    )

    assert any("目录名不安全" in item for item in warnings)
    assert any("源目录已不存在" in item for item in warnings)
    assert sorted(path.name for path in (home / "skills").iterdir()) == ["alpha"]
    assert not (tmp_path / "逃逸").exists()


def test_materialize_rejects_carry_path_traversal(tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    mount = SkillMount(home_name="claude-home", carry=[(str(outside), "../逃逸.json")])

    home = tmp_path / "home"
    warnings = skill_service.materialize(home, mount, [])
    assert any("携带路径不安全" in item for item in warnings)
    assert not (tmp_path / "逃逸.json").exists()


# --- 适配器声明 --------------------------------------------------------------


def test_claude_isolates_skills_strictly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    definition = get_agent("claude-code")

    plain = definition.build_invocation(context())
    assert "CLAUDE_CONFIG_DIR" not in plain.env

    home = tmp_path / "home" / "claude-home"
    ctx = context(skill_home_dir=str(home), workspace_dir=str(tmp_path / "ws"))
    invocation = definition.build_invocation(ctx)
    assert invocation.env["CLAUDE_CONFIG_DIR"] == str(home)
    assert invocation.env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] == "1"

    mount = definition.skill_mount(ctx)
    assert mount.home_name == "claude-home"
    assert [name for _, name in mount.carry] == ["settings.json", "settings.local.json"]
    patch = mount.patch["settings.json"]
    assert patch["disableBundledSkills"] is True
    assert patch["skillOverrides"]["doctor"] == "off"
    assert set(patch["enabledPlugins"].values()) == {False}


def test_claude_warns_about_workspace_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    (workspace / ".claude" / "skills" / "自带").mkdir(parents=True)
    (workspace / ".claude" / "skills" / "自带" / "SKILL.md").write_text(
        "---\nname: 自带\n---\n", encoding="utf-8"
    )

    mount = get_agent("claude-code").skill_mount(context(workspace_dir=str(workspace)))
    assert any(".claude/skills" in note for note in mount.notes)


def test_claude_discovers_personal_and_plugin_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_skill(tmp_path / ".claude" / "skills", "personal")
    plugin_skills = (
        tmp_path / ".claude" / "plugins" / "marketplaces" / "mp" / "plugins" / "p" / "skills"
    )
    write_skill(plugin_skills, "plugin-skill")

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert found["claude-code:personal"].source_kind == "user"
    assert found["claude-code:plugin-skill"].source_kind == "plugin"


def test_codex_uses_isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "codex"
    write_skill(root / "skills", "user-skill")
    write_skill(root / "skills" / ".system", "sys-skill")
    (root / "config.toml").write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    (root / "auth.json").write_text("{}\n", encoding="utf-8")

    definition = get_agent("codex")
    found = {spec.id: spec for spec in definition.discover_skills()}
    assert found["codex:user-skill"].source_kind == "user"
    assert found["codex:sys-skill"].source_kind == "system"

    home = tmp_path / "home" / "codex-home"
    ctx = context(skill_home_dir=str(home))
    assert definition.build_invocation(ctx).env["CODEX_HOME"] == str(home)
    mount = definition.skill_mount(ctx)
    assert sorted(name for _, name in mount.carry) == ["auth.json", "config.toml"]


# --- 接口与端到端 ------------------------------------------------------------


async def make_agent(client: httpx.AsyncClient, type_id: str = "custom-cli") -> dict:
    body: dict = {"name": f"{type_id} 实例", "type_id": type_id}
    if type_id == "custom-cli":
        body["options"] = {"command": "echo"}
    response = await client.post("/api/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def make_role(client: httpx.AsyncClient, agent_id: str) -> dict:
    response = await client.post(
        "/api/roles", json={"name": "审查员", "agent_id": agent_id, "system_prompt": "你是审查员"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把编目结果替换成临时目录里的技能，避免依赖开发机上的真实技能。"""
    root = tmp_path / "catalog"
    write_skill(root, "alpha")
    write_skill(root, "beta")

    def fake() -> dict[str, SkillSpec]:
        # 与真实适配器一致：每次都重新扫描磁盘，目录消失时技能随之失效。
        return {spec.id: spec for spec in scan_skill_root(root, type_id="custom-cli")}

    monkeypatch.setattr(skill_service, "discover_catalog", fake)
    return root


async def test_skills_api_lists_rescans_and_shows_detail(client: httpx.AsyncClient, catalog: Path):
    assert (await client.get("/api/skills")).json()["items"] == []

    result = (await client.post("/api/skills/rescan")).json()
    assert result == {"added": 2, "updated": 0, "missing": 0, "total": 2}

    items = (await client.get("/api/skills")).json()["items"]
    assert [item["slug"] for item in items] == ["alpha", "beta"]
    assert items[0]["source_label"] == "个人技能"
    assert items[0]["file_count"] == 3
    assert items[0]["used_by_roles"] == 0

    filtered = (await client.get("/api/skills", params={"q": "beta"})).json()["items"]
    assert [item["slug"] for item in filtered] == ["beta"]

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert detail["body"].startswith("---")
    assert detail["path"].startswith(str(catalog))
    assert {item["path"] for item in detail["files"]} == {
        "SKILL.md", "scripts/run.sh", "references/note.md",
    }
    assert detail["roles"] == []

    assert (await client.get("/api/skills/nope")).status_code == 404

    # 目录消失后只标记失效，不删记录（角色分配不会断链）。
    skill_service.shutil.rmtree(catalog / "alpha")
    assert (await client.post("/api/skills/rescan")).json()["missing"] == 1
    items = (await client.get("/api/skills")).json()["items"]
    assert next(item for item in items if item["slug"] == "alpha")["available"] is False


async def test_role_skills_come_only_from_assignment(client: httpx.AsyncClient, catalog: Path):
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent["id"])
    assert role["skill_ids"] == []

    response = await client.put(
        f"/api/roles/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["skill_ids"] == ["custom-cli:alpha"]
    assert [brief["name"] for brief in response.json()["skills"]] == ["alpha"]

    roles = (await client.get("/api/roles")).json()["items"]
    listed = next(item for item in roles if item["id"] == role["id"])
    assert listed["skill_ids"] == ["custom-cli:alpha"]

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert [entry["name"] for entry in detail["roles"]] == ["审查员"]

    # 类型不匹配 / 不存在的技能都要被服务端挡住（界面同样禁止选择）。
    codex_agent = await make_agent(client, "codex")
    codex_role = await make_role(client, codex_agent["id"])
    mismatch = await client.put(
        f"/api/roles/{codex_role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert mismatch.status_code == 422
    assert "不匹配" in mismatch.json()["detail"]

    unknown = await client.put(
        f"/api/roles/{role['id']}/skills", json={"skill_ids": ["custom-cli:不存在"]}
    )
    assert unknown.status_code == 422
    assert "不存在" in unknown.json()["detail"]

    # 整表替换：清空分配
    cleared = await client.put(f"/api/roles/{role['id']}/skills", json={"skill_ids": []})
    assert cleared.json()["skill_ids"] == []


async def test_deleted_role_stops_counting_as_a_skill_user(client: httpx.AsyncClient, catalog: Path):
    """软删除的角色不再算技能的"使用中"角色，但分配关系保留，历史对话仍能复现技能。"""
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent["id"])
    await client.put(f"/api/roles/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})
    assert (await client.get("/api/skills/custom-cli:alpha")).json()["used_by_roles"] == 1

    assert (await client.delete(f"/api/roles/{role['id']}")).status_code == 204

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert detail["roles"] == []
    assert detail["used_by_roles"] == 0
    async with SessionLocal() as session:
        assert await skill_service.role_skill_ids(session, role["id"]) == ["custom-cli:alpha"]


async def test_role_copies_skills_on_duplicate_and_drops_mismatch_on_rebind(
    client: httpx.AsyncClient, catalog: Path
):
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    other = await make_agent(client)
    role = await make_role(client, agent["id"])
    await client.put(f"/api/roles/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})

    clone = (await client.post(f"/api/roles/{role['id']}/duplicate")).json()
    assert clone["skill_ids"] == ["custom-cli:alpha"]

    # 改绑到同类型智能体时保留；改绑到不同类型时丢弃不匹配的技能。
    same = (await client.patch(f"/api/roles/{role['id']}", json={"agent_id": other["id"]})).json()
    assert same["skill_ids"] == ["custom-cli:alpha"]

    codex_agent = await make_agent(client, "codex")
    rebound = (
        await client.patch(f"/api/roles/{role['id']}", json={"agent_id": codex_agent["id"]})
    ).json()
    assert rebound["skill_ids"] == []


async def test_run_materializes_assigned_skills_into_isolated_home(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    root = tmp_path / "codex"
    write_skill(root / "skills", "user-skill")
    write_skill(root / "skills" / ".system", "sys-skill")
    (root / "config.toml").write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    (root / "auth.json").write_text("{}\n", encoding="utf-8")

    specs = get_agent("codex").discover_skills()
    monkeypatch.setattr(
        skill_service, "discover_catalog", lambda: {spec.id: spec for spec in specs}
    )
    await client.post("/api/skills/rescan")

    agent = await make_agent(client, "codex")
    role = await make_role(client, agent["id"])
    await client.put(f"/api/roles/{role['id']}/skills", json={"skill_ids": ["codex:user-skill"]})
    conversation = (
        await client.post("/api/conversations", json={"role_id": role["id"], "title": "技能验证"})
    ).json()

    # 模拟「隔离之前就存在的旧会话」：隔离目录里没有它的记录，必须改成新会话，否则续接必然失败。
    async with SessionLocal() as session:
        stored = await session.get(Conversation, conversation["id"])
        stored.session_id = "旧会话-id"
        await session.commit()

    response = await client.post(
        f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"}
    )
    # 不管本机是否装了 codex，技能物化都发生在启动子进程之前，必须已经完成。
    events = await read_sse(response)
    logs = [event.get("text") or "" for event in events if event["type"] == "log"]
    assert any("已改为新会话" in line for line in logs), logs

    home = get_settings().agent_homes_root / conversation["id"] / "codex-home"
    assert (home / "skills" / "user-skill" / "SKILL.md").is_file()
    names = {path.name for path in (home / "skills").iterdir()}
    # 装了 Codex CLI 的机器上，内核启动时会自己预置 ``skills/.system`` 系统技能目录，
    # 那不是角色分配带进去的，所以允许它存在；除此之外必须只有分配到的那个技能。
    assert "user-skill" in names, names
    assert names - {"user-skill", ".system"} == set(), names
    # 源目录里故意放在 ``.system`` 下的未分配技能，绝不能连目录一起被带进来
    # （整目录复制会把它塞进隔离目录，等于「未分配的技能悄悄生效」）。
    assert not (home / "skills" / ".system" / "sys-skill").exists()
    assert (home / "config.toml").is_file()
    assert (home / "auth.json").stat().st_mode & 0o777 == 0o600

    # 删除对话时隔离目录一并回收。
    assert (await client.delete(f"/api/conversations/{conversation['id']}")).status_code == 204
    assert not home.exists()
