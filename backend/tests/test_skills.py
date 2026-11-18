"""技能：解析、统一编目、智能体分配与运行时物化。

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
from helpers import make_gateway, make_profile

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
    ctx = context(home_dir=str(home), workspace_dir=str(tmp_path / "ws"))
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


def test_claude_discovers_personal_and_installed_plugin_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """插件技能只认「已安装清单」里的安装目录，不看市场目录的克隆。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_skill(tmp_path / ".claude" / "skills", "personal")

    plugins = tmp_path / ".claude" / "plugins"
    # 市场目录的克隆：可浏览、未安装 —— 编目时绝不能出现。
    write_skill(plugins / "marketplaces" / "mp" / "plugins" / "p" / "skills", "catalog-only")
    # 真正装上的插件：清单登记在 cache/<市场>/<插件>/<版本>/ 下。
    installed = write_skill(plugins / "cache" / "mp" / "p" / "1.0.0" / "skills", "plugin-skill")
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "p@mp": [
                        {"scope": "user", "installPath": str(installed.parent), "version": "1.0.0"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert found["claude-code:personal"].source_kind == "user"
    # 插件技能带插件名前缀，避免与其它插件的同名技能互相覆盖。
    assert found["claude-code:p-plugin-skill"].source_kind == "plugin"
    assert "claude-code:catalog-only" not in found, "市场目录的克隆不该被编目"


def test_claude_same_name_skills_from_different_plugins_coexist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """两个插件各带一个 access：必须都在，且能被分别调用（目录名不同）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    plugins = tmp_path / ".claude" / "plugins"
    entries = {}
    for channel in ("discord", "telegram"):
        install = plugins / "cache" / "mp" / channel / "1.0.0"
        write_skill(install / "skills", "access")
        entries[f"{channel}@mp"] = [
            {"scope": "user", "installPath": str(install), "version": "1.0.0"}
        ]
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": entries}), encoding="utf-8"
    )

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert "claude-code:discord-access" in found
    assert "claude-code:telegram-access" in found
    # 原始目录名留档，展示名带上来源，列表里不会出现两个一模一样的「access」。
    assert found["claude-code:telegram-access"].extra["original_slug"] == "access"
    assert found["claude-code:discord-access"].name == "access · discord"
    assert found["claude-code:telegram-access"].name == "access · telegram"


def test_plugin_namespace_keeps_unsafe_slugs_and_is_idempotent():
    """前缀不会让不安全的目录名消失，重复应用也不会叠加前缀。"""
    from app.agents.skills import _apply_namespace

    spec = SkillSpec(type_id="t", slug="access", name="access")
    _apply_namespace(spec, "telegram")
    assert spec.slug == "telegram-access"
    _apply_namespace(spec, "telegram")
    assert spec.slug == "telegram-access", "重复应用不能变成 telegram-telegram-access"

    unsafe = SkillSpec(type_id="t", slug="技能 名", name="x")
    _apply_namespace(unsafe, "telegram")
    assert unsafe.slug == "技能 名", "无法安全加前缀时保留原名，不能丢技能"


def test_claude_plugin_catalog_clone_is_never_a_skill_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """没有任何已安装插件时，技能根也不能指向 plugins/（市场克隆所在处）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    plugins = tmp_path / ".claude" / "plugins"
    write_skill(plugins / "marketplaces" / "mp" / "plugins" / "p" / "skills", "catalog-only")

    roots = get_agent("claude-code").skill_roots()
    assert all(Path(root.path) != plugins for root in roots)
    assert all("marketplaces" not in root.path for root in roots)
    assert get_agent("claude-code").discover_skills() == []


def test_claude_empty_manifest_does_not_fall_back_to_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """清单存在但说「没装插件」时，缓存里的残留不能被当成已安装。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    plugins = tmp_path / ".claude" / "plugins"
    # 已卸载插件留在缓存里的目录
    write_skill(plugins / "cache" / "mp" / "leftover" / "1.0.0" / "skills", "ghost-skill")
    (plugins / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {}}), encoding="utf-8"
    )

    assert get_agent("claude-code").discover_skills() == []


def test_claude_plugin_cache_fallback_without_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """旧版本内核没有 installed_plugins.json 时，按 cache 布局兜底扫描（同样加前缀）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".claude" / "plugins" / "cache"
    write_skill(cache / "mp" / "p" / "1.0.0" / "skills", "plugin-skill")

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert found["claude-code:p-plugin-skill"].source_kind == "plugin"


def test_claude_plugin_manifest_survives_broken_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """清单损坏不能让技能页整体失败：退回空列表，个人技能照常编目。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_skill(tmp_path / ".claude" / "skills", "personal")
    state = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    state.parent.mkdir(parents=True)
    state.write_text("{ not json", encoding="utf-8")

    found = {spec.id: spec for spec in get_agent("claude-code").discover_skills()}
    assert found["claude-code:personal"].source_kind == "user"


def test_hermes_marks_bundled_skills_as_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Hermes 的内置技能与用户技能同目录，靠 .bundled_manifest 区分归属。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    skills = tmp_path / "hermes" / "skills"
    write_skill(skills / "apple", "apple-notes")
    write_skill(skills / "mine", "my-own-skill")
    (skills / ".bundled_manifest").write_text("apple-notes:abc123\n", encoding="utf-8")

    found = {spec.id: spec for spec in get_agent("hermes-agent").discover_skills()}
    assert found["hermes-agent:apple-notes"].source_kind == "system"
    assert found["hermes-agent:my-own-skill"].source_kind == "user"


def test_hermes_manifest_ignores_market_and_curator_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """市场技能不能因为名字撞上清单就被标成自带；curator 抑制表也算自带。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    skills = tmp_path / "hermes" / "skills"
    write_skill(skills / ".skillhub", "hub-skill")
    write_skill(skills / "p", "pruned-builtin")
    (skills / ".bundled_manifest").write_text("hub-skill:aaa\n", encoding="utf-8")
    (skills / ".curator_suppressed").write_text("pruned-builtin\n", encoding="utf-8")

    found = {spec.id: spec for spec in get_agent("hermes-agent").discover_skills()}
    assert found["hermes-agent:hub-skill"].source_kind == "market"
    assert found["hermes-agent:pruned-builtin"].source_kind == "system"


def test_hermes_without_manifest_keeps_skills_deletable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """没有清单（旧内核）时退回旧行为：全部当用户技能，至少不会把用户技能锁死。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    write_skill(tmp_path / "hermes" / "skills" / "x", "some-skill")

    found = {spec.id: spec for spec in get_agent("hermes-agent").discover_skills()}
    assert found["hermes-agent:some-skill"].source_kind == "user"


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
    ctx = context(home_dir=str(home))
    assert definition.build_invocation(ctx).env["CODEX_HOME"] == str(home)
    mount = definition.skill_mount(ctx)
    assert sorted(name for _, name in mount.carry) == ["auth.json", "config.toml"]


# --- 接口与端到端 ------------------------------------------------------------


async def make_agent(client: httpx.AsyncClient, type_id: str = "custom-cli") -> dict:
    gateway = await make_gateway(client)
    return await make_profile(
        client,
        name=f"{type_id} 智能体",
        type_id=type_id,
        gateway_id=gateway["id"],
        model=None,
        options={"command": "echo"} if type_id == "custom-cli" else {},
    )


async def make_role(client: httpx.AsyncClient, agent: dict) -> dict:
    return await make_profile(
        client,
        name="审查员",
        type_id=agent["type_id"],
        gateway_id=agent["gateway_id"],
        system_prompt="你是审查员",
        options={"command": "echo"} if agent["type_id"] == "custom-cli" else {},
    )


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
    assert items[0]["used_by_agents"] == 0

    filtered = (await client.get("/api/skills", params={"q": "beta"})).json()["items"]
    assert [item["slug"] for item in filtered] == ["beta"]

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert detail["body"].startswith("---")
    assert detail["path"].startswith(str(catalog))
    assert {item["path"] for item in detail["files"]} == {
        "SKILL.md", "scripts/run.sh", "references/note.md",
    }
    assert detail["agents"] == []

    assert (await client.get("/api/skills/nope")).status_code == 404

    # 目录消失后只标记失效，不删记录（智能体分配不会断链）。
    skill_service.shutil.rmtree(catalog / "alpha")
    assert (await client.post("/api/skills/rescan")).json()["missing"] == 1
    items = (await client.get("/api/skills")).json()["items"]
    assert next(item for item in items if item["slug"] == "alpha")["available"] is False


async def test_removed_market_skills_are_hidden_from_list(client: httpx.AsyncClient):
    """市场技能卸载后（旧数据里可能只标了 available=False）不该继续出现在列表里。"""
    from app.models import Skill

    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:gone-market",
                type_id="custom-cli",
                slug="gone-market",
                name="已卸载的市场技能",
                source_kind="market",
                path="/nonexistent",
                available=False,
            )
        )
        await session.commit()

    items = (await client.get("/api/skills")).json()["items"]
    assert all(item["slug"] != "gone-market" for item in items)
    assert (await client.get("/api/skills", params={"source_kind": "market"})).json()["items"] == []
    # 显式查询失效技能时仍能看到，方便排查历史分配。
    assert [
        item["slug"]
        for item in (await client.get("/api/skills", params={"available": False})).json()["items"]
    ] == ["gone-market"]


async def test_delete_non_system_skill_removes_dir_and_assignment(
    client: httpx.AsyncClient, catalog: Path, monkeypatch: pytest.MonkeyPatch
):
    """个人技能可以从技能页删除：目录、编目行与智能体分配一起消失。"""
    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent)
    assigned = await client.put(
        f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert assigned.status_code == 200, assigned.text
    assert (catalog / "alpha").is_dir()

    response = await client.delete("/api/skills/custom-cli:alpha")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "alpha"
    assert not (catalog / "alpha").exists()
    assert (await client.get("/api/skills/custom-cli:alpha")).status_code == 404
    roles = (await client.get("/api/agents")).json()["items"]
    assert next(item for item in roles if item["id"] == role["id"])["skill_ids"] == []


async def test_delete_refuses_system_skill_and_outside_path(
    client: httpx.AsyncClient, catalog: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """内核自带技能不能删；数据库里的路径越出技能根时也拒绝删除。"""
    from app.models import Skill

    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# 越界技能\n", encoding="utf-8")

    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:builtin",
                type_id="custom-cli",
                slug="builtin",
                name="内核技能",
                source_kind="system",
                path=str(catalog / "alpha"),
            )
        )
        session.add(
            Skill(
                id="custom-cli:outside",
                type_id="custom-cli",
                slug="outside",
                name="越界技能",
                source_kind="user",
                path=str(outside),
            )
        )
        await session.commit()

    response = await client.delete("/api/skills/custom-cli:builtin")
    assert response.status_code == 422
    assert "内核自带" in response.json()["detail"]

    response = await client.delete("/api/skills/custom-cli:outside")
    assert response.status_code == 422
    assert "不在" in response.json()["detail"]
    assert outside.is_dir()


async def test_delete_refuses_installed_plugin_skill(
    client: httpx.AsyncClient, catalog: Path, monkeypatch: pytest.MonkeyPatch
):
    """已安装插件里的技能不能单独删：删了插件就残缺，重扫还会加回来。"""
    from app.models import Skill

    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    async with SessionLocal() as session:
        session.add(
            Skill(
                id="custom-cli:plugin-skill",
                type_id="custom-cli",
                slug="plugin-skill",
                name="插件里的技能",
                source_kind="plugin",
                path=str(catalog / "alpha"),
            )
        )
        await session.commit()

    items = (await client.get("/api/skills")).json()["items"]
    listed = next(item for item in items if item["slug"] == "plugin-skill")
    assert listed["deletable"] is False

    response = await client.delete("/api/skills/custom-cli:plugin-skill")
    assert response.status_code == 422
    assert "插件" in response.json()["detail"]
    assert (catalog / "alpha").is_dir()


async def test_delete_missing_skill_returns_404(client: httpx.AsyncClient):
    """不存在的技能返回 404（不靠中文字符串匹配状态码）。"""
    response = await client.delete("/api/skills/custom-cli:missing")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


async def test_delete_reports_directory_failure(
    client: httpx.AsyncClient, catalog: Path, monkeypatch: pytest.MonkeyPatch
):
    """磁盘删不掉时要显式报错，不能假装成功（否则重扫会把技能加回来）。"""
    monkeypatch.setattr(
        skill_service, "skill_roots_for_type", lambda _type_id: [skill_service.SkillRoot(str(catalog))]
    )
    await client.post("/api/skills/rescan")

    def refuse(_path, *_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(skill_service.shutil, "rmtree", refuse)
    response = await client.delete("/api/skills/custom-cli:alpha")
    assert response.status_code == 422
    assert "删除" in response.json()["detail"]
    assert (catalog / "alpha").is_dir()
    # 编目行还在：失败不能留下「数据库已删、目录还在」的半截状态。
    assert (await client.get("/api/skills/custom-cli:alpha")).status_code == 200


async def test_agent_skills_come_only_from_assignment(client: httpx.AsyncClient, catalog: Path):
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent)
    assert role["skill_ids"] == []

    response = await client.put(
        f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["skill_ids"] == ["custom-cli:alpha"]
    assert [brief["name"] for brief in response.json()["skills"]] == ["alpha"]

    roles = (await client.get("/api/agents")).json()["items"]
    listed = next(item for item in roles if item["id"] == role["id"])
    assert listed["skill_ids"] == ["custom-cli:alpha"]

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert [entry["name"] for entry in detail["agents"]] == ["审查员"]

    # 类型不匹配 / 不存在的技能都要被服务端挡住（界面同样禁止选择）。
    codex_agent = await make_agent(client, "codex")
    codex_role = await make_role(client, codex_agent)
    mismatch = await client.put(
        f"/api/agents/{codex_role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]}
    )
    assert mismatch.status_code == 422
    assert "不匹配" in mismatch.json()["detail"]

    unknown = await client.put(
        f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:不存在"]}
    )
    assert unknown.status_code == 422
    assert "不存在" in unknown.json()["detail"]

    # 整表替换：清空分配
    cleared = await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": []})
    assert cleared.json()["skill_ids"] == []


async def test_deleted_agent_stops_counting_as_a_skill_user(client: httpx.AsyncClient, catalog: Path):
    """软删除的智能体不再算技能的"使用中"智能体，但分配关系保留，历史对话仍能复现技能。"""
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    role = await make_role(client, agent)
    await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})
    assert (await client.get("/api/skills/custom-cli:alpha")).json()["used_by_agents"] == 1

    assert (await client.delete(f"/api/agents/{role['id']}")).status_code == 204

    detail = (await client.get("/api/skills/custom-cli:alpha")).json()
    assert detail["agents"] == []
    assert detail["used_by_agents"] == 0
    async with SessionLocal() as session:
        assert await skill_service.agent_skill_ids(session, role["id"]) == ["custom-cli:alpha"]


async def test_agent_copies_skills_on_duplicate_and_drops_mismatch_on_retype(
    client: httpx.AsyncClient, catalog: Path
):
    await client.post("/api/skills/rescan")
    agent = await make_agent(client)
    other = await make_agent(client)
    role = await make_role(client, agent)
    await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": ["custom-cli:alpha"]})

    clone = (await client.post(f"/api/agents/{role['id']}/duplicate")).json()
    assert clone["skill_ids"] == ["custom-cli:alpha"]

    # 换网关（内核不变）时保留；换内核时丢弃不匹配的技能。
    same = (
        await client.patch(f"/api/agents/{role['id']}", json={"gateway_id": other["gateway_id"]})
    ).json()
    assert same["skill_ids"] == ["custom-cli:alpha"]

    rebound = (await client.patch(f"/api/agents/{role['id']}", json={"type_id": "codex"})).json()
    assert rebound["skill_ids"] == []


async def test_run_materializes_assigned_skills_into_isolated_home(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    # 本用例只验证「启动子进程之前已经物化」，不需要真实内核跑完一轮；
    # 给它一个很短的运行上限，免得本机装了 codex 时把测试拖成无限等待。
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 5)
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
    role = await make_role(client, agent)
    await client.put(f"/api/agents/{role['id']}/skills", json={"skill_ids": ["codex:user-skill"]})
    conversation = (
        await client.post("/api/conversations", json={"agent_profile_id": role["id"], "title": "技能验证"})
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
    # 那不是智能体分配带进去的，所以允许它存在；除此之外必须只有分配到的那个技能。
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


def test_materialize_syncs_frontmatter_name_to_directory(tmp_path: Path):
    """物化副本的 frontmatter name 要与目录名一致，否则按名字找技能的内核会找不到。"""
    from app.runtime.skills import materialize

    source = write_skill(tmp_path / "src", "access")
    spec = spec_for(source, source_kind="plugin")
    spec.slug = "telegram-access"  # 模拟命名空间前缀后的目录名

    home = tmp_path / "home"
    warnings = materialize(home, SkillMount(home_name="h"), [spec])

    assert warnings == []
    copied = (home / "skills" / "telegram-access" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: telegram-access" in copied
    # 只改副本：源目录必须原样保留。
    assert "name: access" in (source / "SKILL.md").read_text(encoding="utf-8")
    # 其它元数据不能被顺手改掉。
    assert "description: access 的说明" in copied


def test_materialize_leaves_matching_name_untouched(tmp_path: Path):
    """目录名与 frontmatter 一致时不该改写文件（避免无谓地动别人的技能）。"""
    from app.runtime.skills import materialize

    source = write_skill(tmp_path / "src", "plain")
    original = (source / "SKILL.md").read_text(encoding="utf-8")
    home = tmp_path / "home"

    assert materialize(home, SkillMount(home_name="h"), [spec_for(source)]) == []
    assert (home / "skills" / "plain" / "SKILL.md").read_text(encoding="utf-8") == original


def test_seed_skills_is_idempotent_and_skips_when_present(tmp_path: Path, monkeypatch):
    """内核自带技能播种：已存在就跳过，绝不重复执行子进程。"""
    from app.agents.builtin import codex as codex_mod

    home = tmp_path / "codex-home"
    monkeypatch.setattr(codex_mod, "codex_home", lambda: home)

    called: list[list[str]] = []

    def fake_run(argv, **kwargs):
        called.append(argv)

        class Result:
            returncode = 0
            stdout = b""
            stderr = b""

        return Result()

    monkeypatch.setattr(codex_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(codex_mod.shutil, "which", lambda _: "/usr/bin/codex")

    agent = get_agent("codex")

    # 目录不存在 → 会跑一次子进程播种
    (home / "skills").mkdir(parents=True)
    actions = agent.seed_skills()
    assert len(called) == 1, called
    assert called[0][:3] == ["codex", "debug", "prompt-input"]
    # 假子进程没真的写目录，所以这里不产生动作描述
    assert actions == []

    # 目录已存在 → 直接跳过，不再起子进程
    (home / "skills" / ".system").mkdir()
    called.clear()
    assert agent.seed_skills() == []
    assert called == [], "已有 .system 时不应再执行播种命令"


def test_seed_skills_never_raises_when_binary_missing(tmp_path: Path, monkeypatch):
    """没装内核时播种必须静默返回空列表，不能把启动或安装搞挂。"""
    from app.agents.builtin import codex as codex_mod

    monkeypatch.setattr(codex_mod, "codex_home", lambda: tmp_path / "h")
    monkeypatch.setattr(codex_mod.shutil, "which", lambda _: None)

    assert get_agent("codex").seed_skills() == []
