"""技能：统一编目、智能体分配与运行时物化。

* 内核自带 / 宿主机上的技能由适配器的 :meth:`AgentDefinition.discover_skills` 只读扫描，
  全平台共享；
* 用户自己的技能（导入 / 市场安装 / 跨内核复制）存放在
  ``<数据目录>/skill-libraries/<owner_id>/<type_id>/<slug>``，按用户隔离；
* 两类技能都汇进 ``skills`` 表，并按智能体的分配结果在运行时物化到该内核的隔离目录，
  从而保证「智能体能用的技能 == 分配到的技能」。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, all_agents, get_agent
from ..agents.skills import (
    LIBRARY_ID_MARK,
    MARKET_MARKER_FILE,
    MARKET_SOURCE,
    MAX_SKILL_BYTES,
    SKILL_FILE,
    SKIP_DIRS,
    MarketInstallInfo,
    SkillMount,
    SkillRoot,
    SkillSpec,
    is_library_skill_id,
    library_skill_id,
    read_market_marker,
    safe_skill_dirname,
    scan_skill_root,
    write_market_marker,
)
from ..models import AgentProfile, AgentSkill, Skill
from ..settings import get_settings
from ..skillhub import MAX_ARCHIVE_BYTES, SkillHubError, extract_skill_archive
from .isolation import ensure_home, home_for, note_fresh_home, safe_relative

logger = logging.getLogger("arkswarm")

#: 技能来源的中文标签
SOURCE_LABELS = {
    "user": "个人技能",
    "project": "项目技能",
    "plugin": "插件技能",
    "system": "内核系统技能",
    "manual": "平台技能",
    MARKET_SOURCE: "腾讯 SkillHub",
}

#: 同名技能的去重优先级（越小越优先）；市场技能是用户显式安装的，优先级最高
SOURCE_PRECEDENCE = {
    "manual": 0,
    MARKET_SOURCE: 1,
    "user": 2,
    "project": 3,
    "plugin": 4,
    "system": 5,
}

#: 详情页读取 SKILL.md 正文的上限
MAX_BODY_BYTES = 200_000

#: 没有真实用户（鉴权关闭的单用户部署）时，用户技能库用的目录键。
LOCAL_OWNER_KEY = "local"
_OWNER_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def owner_key(owner_id: str | None) -> str:
    """把用户 id 变成安全的目录名；没有用户时落到 ``local``（单用户部署）。"""
    if not owner_id:
        return LOCAL_OWNER_KEY
    if not _OWNER_KEY.match(owner_id):
        raise SkillError(f"用户标识不合法：{owner_id}")
    return owner_id


def library_root(owner_id: str | None, type_id: str) -> Path:
    """某个用户的某个内核的技能库目录（平台托管，按用户隔离）。"""
    return get_settings().skill_libraries_root / owner_key(owner_id) / type_id


def is_library_row(row: Skill) -> bool:
    """这一行是否是平台托管的用户技能（区别于内核自带 / 宿主机共享技能）。"""
    return is_library_skill_id(row.id)


# --- 编目 -------------------------------------------------------------------


def _annotate_library_spec(spec: SkillSpec, *, owner_id: str | None) -> SkillSpec:
    """给从用户技能库扫出来的技能补上归属与来源。

    库里带市场安装标记（``.skillhub.json``）的是「市场技能」，其余是用户自己放的「个人技能」。
    """
    spec.owner_id = owner_id
    spec.library = True
    info = read_market_marker(spec.path)
    if info is not None:
        spec.source_kind = MARKET_SOURCE
        spec.source_slug = info.slug or spec.slug
        spec.source_url = info.source_url
    else:
        spec.source_kind = "user"
    return spec


def discover_library_skills(owners: Iterable[str | None]) -> dict[str, SkillSpec]:
    """扫描这些用户的技能库（``<用户>/<内核>/<技能>``），返回按标识去重后的技能。

    目录布局是平台自己的约定，所以按目录名发现内核、再用 ``skill_roots_for_type``
    校验它确实支持技能；单个用户 / 内核目录出错不影响其它目录。
    """
    catalog: dict[str, SkillSpec] = {}
    for owner_id in owners:
        owner_dir = get_settings().skill_libraries_root / owner_key(owner_id)
        try:
            type_dirs = sorted(
                child for child in owner_dir.iterdir() if child.is_dir()
            )
        except OSError:
            continue
        for type_dir in type_dirs:
            try:
                skill_roots_for_type(type_dir.name)
            except SkillError:
                continue
            try:
                specs = scan_skill_root(type_dir, type_id=type_dir.name, source_kind="user")
            except Exception as exc:  # noqa: BLE001 - 单个内核目录坏了不能拖垮整个目录
                logger.warning("扫描用户技能库 %s 失败：%s", type_dir, exc)
                continue
            for spec in specs:
                spec = _annotate_library_spec(spec, owner_id=owner_id)
                catalog[spec.id] = spec
    return catalog


def discover_catalog() -> dict[str, SkillSpec]:
    """聚合所有适配器声明的技能；单个适配器出错不影响其余技能。"""
    catalog: dict[str, SkillSpec] = {}
    for definition in all_agents():
        try:
            specs = definition.discover_skills() or []
        except Exception as exc:  # noqa: BLE001 - 扫描失败不能拖垮整个技能目录
            logger.warning("扫描智能体 %s 的技能失败：%s", definition.id, exc)
            continue
        for spec in specs:
            current = catalog.get(spec.id)
            if current is None or _rank(spec) < _rank(current):
                catalog[spec.id] = spec
    return catalog


def _rank(spec: SkillSpec) -> int:
    return SOURCE_PRECEDENCE.get(spec.source_kind, 9)


async def _library_owners(session: AsyncSession) -> list[str | None]:
    """要扫描哪些用户的技能库：磁盘上的目录 + 数据库里登记过的归属。"""
    keys: set[str] = set()
    root = get_settings().skill_libraries_root
    try:
        if root.is_dir():
            keys.update(
                child.name
                for child in root.iterdir()
                if child.is_dir() and _OWNER_KEY.match(child.name)
            )
    except OSError:
        pass
    # 目录暂时不见、但库里还登记着的技能也要扫一次（这样它们才会被标记为失效）；
    # 不扫的话它们会永远停在「可用」状态。
    statement = select(Skill.owner_id).where(Skill.id.like(f"%{LIBRARY_ID_MARK}%")).distinct()
    for (owner_id,) in (await session.execute(statement)).all():
        try:
            keys.add(owner_key(owner_id))
        except SkillError:
            # 库里的归属被改坏时跳过这一行，不能让整个编目失败。
            continue
    return [None if key == LOCAL_OWNER_KEY else key for key in sorted(keys)]


async def rescan(session: AsyncSession) -> dict[str, int]:
    """重新扫描内核技能目录与各用户的技能库，并同步到 ``skills`` 表。

    目录消失的技能只标记 ``available=False``，不删除记录，避免智能体分配断链。
    """
    discovered = discover_catalog()
    discovered.update(discover_library_skills(await _library_owners(session)))
    rows = {row.id: row for row in (await session.execute(select(Skill))).scalars().all()}
    now = datetime.now(timezone.utc)
    added = updated = 0

    for skill_id, spec in discovered.items():
        row = rows.get(skill_id)
        fields = spec.to_row()
        if row is None:
            session.add(Skill(**fields, available=True, discovered_at=now, updated_at=now))
            added += 1
            continue
        changed = any(getattr(row, key) != value for key, value in fields.items())
        if changed or not row.available:
            for key, value in fields.items():
                setattr(row, key, value)
            row.available = True
            row.updated_at = now
            updated += 1

    missing = 0
    for skill_id, row in rows.items():
        if skill_id not in discovered and row.available:
            row.available = False
            row.updated_at = now
            missing += 1

    await session.commit()
    return {"added": added, "updated": updated, "missing": missing, "total": len(discovered)}


async def list_rows(
    session: AsyncSession,
    *,
    type_id: str | None = None,
    source_kind: str | None = None,
    available: bool | None = None,
    query: str | None = None,
    owner_id: str | None = None,
    all_owners: bool = False,
) -> list[Skill]:
    """列出技能；``all_owners=False`` 时只返回共享技能 + 指定用户自己的技能。"""
    statement = select(Skill).order_by(Skill.type_id, Skill.source_kind, Skill.name)
    if not all_owners:
        if owner_id is None:
            statement = statement.where(Skill.owner_id.is_(None))
        else:
            statement = statement.where(
                or_(Skill.owner_id.is_(None), Skill.owner_id == owner_id)
            )
    if type_id:
        statement = statement.where(Skill.type_id == type_id)
    if source_kind:
        statement = statement.where(Skill.source_kind == source_kind)
    if available is not None:
        statement = statement.where(Skill.available == available)  # noqa: E712
    else:
        # 市场技能被卸载、插件技能随插件卸载后目录就没了，它们不该再出现在技能列表里
        # （用户看到的「已卸载」）。内核自带技能目录消失时仍保留一行并显示「目录已消失」，
        # 提示分配关系需要清理。
        statement = statement.where(
            or_(
                Skill.source_kind.not_in(HIDDEN_WHEN_MISSING),  # type: ignore[attr-defined]
                Skill.available == True,  # noqa: E712
            )
        )
    rows = (await session.execute(statement)).scalars().all()
    if query:
        needle = query.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.name.lower()
            or needle in row.slug.lower()
            or needle in (row.description or "").lower()
        ]
    return list(rows)


async def get_row(session: AsyncSession, skill_id: str) -> Skill | None:
    return await session.get(Skill, skill_id)


#: 由别人（内核或插件管理器）托管的来源：目录归它们所有，平台只能读、不能删。
#: ``plugin`` 是「已安装插件」里的技能 —— 单独删一个技能会让那个插件残缺，
#: 要卸载应该走插件管理器整包卸载。
READONLY_SOURCES = frozenset({"system", "plugin"})

#: 目录消失后就不该再出现在列表里的来源：它们跟着「卸载」动作整体消失，
#: 留一行「目录已消失」只会误导（也删不掉）。内核自带技能仍保留，提示要清理分配。
HIDDEN_WHEN_MISSING = frozenset({MARKET_SOURCE, "plugin"})


def is_deletable(row: Skill) -> bool:
    """只有「目录归平台管」的技能允许从技能页删除。

    内核自带（``system``）与已安装插件（``plugin``）里的技能只读：前者删了内核就残缺，
    后者删了插件就残缺，而且重扫还会把它们加回来（用户会看到「删不掉」）。
    """
    return row.source_kind not in READONLY_SOURCES


async def usage_counts(
    session: AsyncSession, owner_id: str | None = None
) -> dict[str, int]:
    """每个技能被多少个智能体使用（已软删除的智能体不再算"使用中"）。

    ``owner_id`` 非空时只数该用户的智能体（成员看自己的用量，管理员传 ``None`` 看全局）。
    """
    statement = (
        select(AgentSkill.skill_id)
        .join(AgentProfile, AgentProfile.id == AgentSkill.agent_profile_id)
        .where(AgentProfile.deleted_at.is_(None))
    )
    if owner_id is not None:
        statement = statement.where(AgentProfile.owner_id == owner_id)
    counts: dict[str, int] = {}
    for skill_id in (await session.execute(statement)).scalars().all():
        counts[skill_id] = counts.get(skill_id, 0) + 1
    return counts


async def agents_using(
    session: AsyncSession, skill_id: str, owner_id: str | None = None
) -> list[AgentProfile]:
    statement = (
        select(AgentProfile)
        .join(AgentSkill, AgentSkill.agent_profile_id == AgentProfile.id)
        .where(AgentSkill.skill_id == skill_id, AgentProfile.deleted_at.is_(None))
        .order_by(AgentProfile.created_at)
    )
    if owner_id is not None:
        statement = statement.where(AgentProfile.owner_id == owner_id)
    return list((await session.execute(statement)).scalars().all())


def read_body(skill_path: str) -> tuple[str, bool]:
    """读取 SKILL.md 正文（供详情页展示）；返回 ``(正文, 是否被截断)``。

    ``skill_path`` 既接受技能目录（读目录下的 ``SKILL.md``），也接受 ``SKILL.md`` 文件本身。
    """
    base = Path(skill_path)
    target = base / SKILL_FILE if base.is_dir() or not base.suffix else base
    try:
        data = target.read_bytes()
    except OSError:
        return "", False
    truncated = len(data) > MAX_BODY_BYTES
    return data[:MAX_BODY_BYTES].decode("utf-8", "replace"), truncated


# --- 分配 -------------------------------------------------------------------


async def agent_skill_ids(session: AsyncSession, profile_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(AgentSkill).where(AgentSkill.agent_profile_id == profile_id).order_by(AgentSkill.position)
        )
    ).scalars().all()
    return [row.skill_id for row in rows]


async def set_agent_skills(
    session: AsyncSession, profile: AgentProfile, skill_ids: list[str]
) -> list[str]:
    """整表替换智能体的技能分配；返回实际写入的 skill_id 列表。

    只接受「与智能体内核一致且可用」的技能：界面已经禁止选择不匹配的技能，
    这里是服务端兜底。
    """
    wanted = list(dict.fromkeys(skill_ids))
    for skill_id in wanted:
        row = await session.get(Skill, skill_id)
        if row is None:
            raise ValueError(f"技能不存在：{skill_id}")
        if row.type_id != profile.type_id:
            raise ValueError(
                f"技能「{row.name}」属于 {row.type_id}，"
                f"与该智能体的内核（{profile.type_id}）不匹配"
            )
        # 用户技能库按用户隔离：智能体只能分配「共享技能 + 自己主人的技能」。
        if row.owner_id is not None and row.owner_id != profile.owner_id:
            raise ValueError(f"技能「{row.name}」不在该智能体的技能库里，无法分配")

    for link in (
        await session.execute(select(AgentSkill).where(AgentSkill.agent_profile_id == profile.id))
    ).scalars().all():
        await session.delete(link)
    await session.flush()

    for position, skill_id in enumerate(wanted):
        session.add(AgentSkill(agent_profile_id=profile.id, skill_id=skill_id, position=position))
    await session.commit()
    return wanted


async def skills_for_agent(session: AsyncSession, profile: AgentProfile) -> list[SkillSpec]:
    """智能体在本轮运行中真正生效的技能（只取同内核且目录仍在的）。"""
    ids = await agent_skill_ids(session, profile.id)
    if not ids:
        return []
    rows = (await session.execute(select(Skill).where(Skill.id.in_(ids)))).scalars().all()
    by_id = {row.id: row for row in rows}
    specs: list[SkillSpec] = []
    for skill_id in ids:
        row = by_id.get(skill_id)
        if row is None or not row.available or row.type_id != profile.type_id:
            continue
        # 旧分配关系如果指向了别人的技能，运行时也必须挡住（按用户隔离）。
        if row.owner_id is not None and row.owner_id != profile.owner_id:
            continue
        specs.append(spec_from_row(row))
    return specs


def spec_from_row(row: Skill) -> SkillSpec:
    return SkillSpec(
        type_id=row.type_id,
        slug=row.slug,
        name=row.name,
        owner_id=row.owner_id,
        library=is_library_row(row),
        description=row.description or "",
        source_kind=row.source_kind,
        path=row.path,
        version=row.version,
        author=row.author,
        when_to_use=row.when_to_use,
        allowed_tools=list(row.allowed_tools or []),
        total_bytes=row.total_bytes or 0,
        fingerprint=row.fingerprint or "",
        note=row.note,
        source_slug=row.slug if row.source_kind == MARKET_SOURCE else None,
        source_url=(row.source_url or None),
    )


# --- 物化 -------------------------------------------------------------------


async def prepare_skill_home(
    session: AsyncSession,
    definition: AgentDefinition,
    profile: AgentProfile,
    conversation: Any,
    ctx: AgentContext,
    *,
    scope_id: str | None = None,
) -> list[str]:
    """把智能体分配到的技能物化进该内核的隔离目录，并把结果写回 ``ctx``。

    ``scope_id`` 是隔离目录的作用域（默认用对话 id；协作运行会传自己的作用域）。
    传了 ``scope_id`` 时 ``conversation`` 可以为 ``None``（协作运行没有对话）。
    返回需要提示给用户的说明/告警（由运行层作为 ``log`` 事件暴露）。
    """
    ctx.skills = await skills_for_agent(session, profile)
    mount = definition.skill_mount(ctx)
    if mount is None:
        return []
    # 技能与 MCP 共用同一个「家」：MCP 物化（prepare_mcp_home）会写进同一个目录，
    # 所以这里定下的 home_name 就是内核的隔离配置目录名。
    home = home_for(scope_id or conversation.id, mount.home_name)
    if ctx.home_dir and Path(ctx.home_dir) != home:
        # 上一轮（或另一段物化）已经定过目录：以它为准，避免技能与 MCP 各写一个家。
        home = Path(ctx.home_dir)
    first_run = not home.exists()
    warnings = materialize(home, mount, ctx.skills)
    ctx.home_dir = str(home)
    warnings += note_fresh_home(ctx, first_run=first_run)
    return [*mount.notes, *warnings]


def materialize(home: Path, mount: SkillMount, specs: Iterable[SkillSpec]) -> list[str]:
    """按物化计划把技能目录与内核配置写进隔离目录；返回告警列表。"""
    warnings = ensure_home(home)
    if warnings:
        return warnings

    skills_root = home / mount.skills_subdir
    # 每次运行都重建受管技能目录：取消分配后技能必须立刻消失。
    if skills_root.exists():
        shutil.rmtree(skills_root, ignore_errors=True)
    try:
        skills_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [f"无法创建技能目录 {skills_root}：{exc}"]

    for spec in specs:
        name = safe_skill_dirname(spec.slug)
        source = Path(spec.path) if spec.path else None
        if name is None:
            warnings.append(f"技能「{spec.name}」的目录名不安全，已跳过")
            continue
        if source is None or not source.is_dir():
            warnings.append(f"技能「{spec.name}」的源目录已不存在：{spec.path}")
            continue
        if spec.total_bytes > MAX_SKILL_BYTES:
            warnings.append(
                f"技能「{spec.name}」约 {spec.total_bytes // 1024} KB，超过单技能上限，已跳过"
            )
            continue
        target = skills_root / name
        try:
            shutil.copytree(source, target, symlinks=False, dirs_exist_ok=True)
        except OSError as exc:
            warnings.append(f"技能「{spec.name}」复制失败：{exc}")
            continue
        # 只在「名字被改过」时同步 frontmatter：加了命名空间的技能 slug 与源目录名不同，
        # 其余情况（市场技能常见的 `目录 kdocs-skill / name: kdocs`）必须原样保留。
        if name != source.name:
            _sync_frontmatter_name(target / SKILL_FILE, name, warnings)

    for source_path, dest_rel in mount.carry:
        _carry_file(home, source_path, dest_rel, warnings)

    for dest_rel, patch in mount.patch.items():
        _patch_json(home / dest_rel, patch, warnings)

    return warnings


def _sync_frontmatter_name(path: Path, name: str, warnings: list[str]) -> None:
    """让物化副本的 frontmatter ``name`` 与目录名一致（尽力而为）。

    内核之间认技能的方式不同：Claude Code 按**目录名**调用，Hermes 按 frontmatter
    的 ``name`` 查找。加了命名空间的技能目录叫 ``telegram-access``，若文件里仍写着
    ``name: access``，Hermes 就找不到它、Claude Code 也会报「名字对不上」。

    只改物化出来的**副本**，源目录始终只读。改写失败不影响复制结果（技能照旧可用），
    但会在告警里说明，避免「明明分配了却调不到」。
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return
    for index in range(1, closing):
        if not lines[index].startswith("name:"):
            continue
        current = lines[index].split(":", 1)[1].strip().strip("\"'")
        if current == name:
            return
        lines[index] = f"name: {name}\n"
        try:
            path.write_text("".join(lines), encoding="utf-8")
        except OSError as exc:
            warnings.append(f"技能「{name}」的 SKILL.md 改名失败：{exc}")
        return


def _carry_file(home: Path, source_path: str, dest_rel: str, warnings: list[str]) -> None:
    target_rel = _safe_relative(dest_rel)
    if target_rel is None:
        warnings.append(f"内核配置的携带路径不安全，已跳过：{dest_rel}")
        return
    source = Path(source_path)
    if not source.is_file():
        return
    target = home / target_rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
    except OSError as exc:
        warnings.append(f"携带内核配置 {dest_rel} 失败：{exc}")


def _patch_json(path: Path, patch: dict[str, Any], warnings: list[str]) -> None:
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            warnings.append(f"内核配置 {path.name} 无法解析，已按空配置重建")
            data = {}
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            merged = dict(data[key])
            merged.update(value)
            data[key] = merged
        else:
            data[key] = value
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        warnings.append(f"写入内核配置 {path.name} 失败：{exc}")


#: 兼容旧调用点：路径安全校验的实现已挪到 :mod:`runtime.isolation`（技能与 MCP 共用一份）。
_safe_relative = safe_relative


def cleanup_conversation(conversation_id: str) -> None:
    """删除对话时清掉它的隔离配置目录（尽力而为）。"""
    home = get_settings().agent_homes_root / conversation_id
    shutil.rmtree(home, ignore_errors=True)


# --- 技能市场（安装 / 卸载）--------------------------------------------------


class SkillError(Exception):
    """技能操作失败：消息面向用户。"""


class SkillNotFound(SkillError):
    """技能不存在（API 层据此映射 404，不靠中文字符串匹配）。"""


def skill_roots_for_type(type_id: str) -> list[SkillRoot]:
    """某个内核声明的技能根；未知内核或该内核不支持技能时抛 :class:`SkillError`。"""
    try:
        definition = get_agent(type_id)
    except KeyError:
        raise SkillError(f"未知的智能体类型：{type_id}") from None
    roots = definition.skill_roots()
    if not roots:
        raise SkillError(f"{definition.name} 不支持技能：没有声明技能目录")
    return roots


def market_install_root(type_id: str, owner_id: str | None) -> str:
    """市场技能要落到的目录：当前用户的该内核技能库。

    技能库由平台托管（不再写进内核全局目录），这样市场安装天然按用户隔离。
    """
    return str(library_root(owner_id, type_id))


def _is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _delete_tree(target: Path, name: str) -> None:
    """删掉一个技能目录；失败要显式暴露。

    不能 ``ignore_errors=True``：删到一半失败时接口会回「已删除」，但目录还在，
    下一次重扫又把它加回编目 —— 用户会看到技能删不掉却没有任何提示。
    """
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        # 检查与删除之间目录被别的进程删掉了：结果与目标一致，视为成功。
        return
    except OSError as exc:
        raise SkillError(f"删除「{name}」的目录失败：{exc}（请检查文件权限后重试）") from exc


def _remove_installed_skill(type_id: str, row: Skill) -> None:
    """卸载一个市场技能：只删「我们装进去的目录」，越界或非市场技能一律拒绝。

    判据有两条，缺一不可：目录里有市场安装标记（``.skillhub.json``），且目录落在
    该内核声明的可写技能根内。这样即便数据库被改成指向别处，也不会误删内核自带技能。
    """
    if row.source_kind != MARKET_SOURCE:
        raise SkillError(f"「{row.name}」不是市场技能，不能卸载")
    target = Path(row.path)
    if not target.is_dir():
        return
    if read_market_marker(target) is None:
        raise SkillError(
            f"「{row.name}」缺少市场安装标记，拒绝删除（可能不是平台安装的技能）"
        )
    if is_library_row(row):
        allowed = _is_declared_skill_dir(row, target)
    else:
        # 兼容升级前装进内核原生目录的老市场技能：仍按适配器声明的可写根校验。
        allowed = any(
            root.installable and _is_under(Path(root.path), target)
            for root in skill_roots_for_type(type_id)
        )
    if not allowed:
        raise SkillError(f"「{row.name}」不在可写入的技能目录内，拒绝删除")
    _delete_tree(target, row.name)


def _remove_skill_directory(row: Skill) -> None:
    """删掉技能自己的源目录；只允许删该技能所属技能根 / 用户技能库之下的目录。

    这是技能页「删除」的磁盘侧护栏：数据库里的路径即便被改坏，也只能删到允许的目录内部，
    并且目标必须真的像一个技能目录（含 ``SKILL.md``、不是符号链接、不是技能根本身）。
    """
    target = Path(row.path)
    if not target.exists():
        return
    if target.is_symlink() or not target.is_dir():
        raise SkillError(f"「{row.name}」的路径不是一个技能目录，拒绝删除")
    if not (target / SKILL_FILE).is_file():
        raise SkillError(f"「{row.name}」目录内没有 {SKILL_FILE}，拒绝删除")
    if not _is_declared_skill_dir(row, target):
        raise SkillError(f"「{row.name}」不在它所属的技能目录内，拒绝删除")
    _delete_tree(target, row.name)


async def _drop_skill_row(session: AsyncSession, row: Skill) -> None:
    """断开智能体分配并删除编目行，然后重扫（目录已删时不会把技能加回来）。"""
    await session.execute(delete(AgentSkill).where(AgentSkill.skill_id == row.id))
    await session.delete(row)
    await session.commit()
    await rescan(session)


async def delete_skill(session: AsyncSession, skill_id: str) -> str:
    """删除一个由平台管理的技能（个人 / 项目 / 市场…），返回技能名。"""
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    if not is_deletable(row):
        if row.source_kind == "plugin":
            raise SkillError(
                f"「{row.name}」属于已安装的插件，不能单独删除；请在插件管理器里卸载整个插件"
            )
        raise SkillError(f"「{row.name}」是内核自带技能，不能删除")
    name = row.name
    _remove_skill_directory(row)
    await _drop_skill_row(session, row)
    return name


async def wait_for_market_registration(
    session: AsyncSession, skill_id: str, *, attempts: int = 3, interval: float = 0.4
) -> Skill | None:
    """安装落盘后等编目把它登记进 ``skills`` 表（文件系统可见性有轻微延迟）。"""
    for _ in range(max(1, attempts)):
        row = await get_row(session, skill_id)
        if row is not None:
            return row
        await asyncio.sleep(interval)
    return await get_row(session, skill_id)


async def install_market_skill(
    session: AsyncSession,
    *,
    type_id: str,
    slug: str,
    version: str | None,
    archive: bytes,
    owner_id: str | None = None,
    source_url: str | None = None,
    display_slug: str | None = None,
) -> dict[str, Any]:
    """把一个市场技能的 zip 落盘到**当前用户**的技能库，并同步进统一编目。

    步骤：安全解压到 ``<技能库>/<type_id>/<slug>`` → 写入市场安装标记 → 触发一次重扫。
    返回 ``{"skill_id", "name", "warnings"}``；失败抛 :class:`SkillError`。
    """
    safe = safe_skill_dirname(slug)
    if safe is None:
        raise SkillError(f"技能标识不合法：{slug}")
    skill_roots_for_type(type_id)  # 未知内核 / 不支持技能的内核先挡住
    root = Path(market_install_root(type_id, owner_id))
    target = root / safe
    try:
        root.mkdir(parents=True, exist_ok=True)
        extract_skill_archive(archive, target)
    except OSError as exc:
        raise SkillError(f"安装失败：无法写入技能目录（{exc}）") from None

    write_market_marker(
        target,
        MarketInstallInfo(
            slug=display_slug or safe,
            version=version,
            source_url=source_url,
            installed_at=datetime.now(timezone.utc).isoformat(),
        ),
    )

    await rescan(session)
    skill_id = library_skill_id(type_id, safe, owner_id)
    row = await wait_for_market_registration(session, skill_id)
    if row is None:
        # 目录已落盘，但编目还没收录（通常是该目录被别的技能根重复覆盖）——给出可操作提示。
        raise SkillError(
            f"技能已解压到 {target}，但编目未收录；"
            "请到「技能」页点一次「重新扫描」"
        )
    return {"skill_id": row.id, "name": row.name, "warnings": []}


async def uninstall_market_skill(session: AsyncSession, skill_id: str) -> str:
    """卸载一个市场技能：删掉安装目录、断开分配并移除编目记录。返回被删技能名。"""
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    name = row.name
    _remove_installed_skill(row.type_id, row)
    # 卸载 = 从平台移除：先断开「智能体 → 技能」的分配（外键是 RESTRICT），
    # 再删编目行。否则技能会以「目录已消失」的形式继续留在技能页与市场已装列表里。
    await _drop_skill_row(session, row)
    return name


# --- 导出 / 导入 / 跨内核复制 -------------------------------------------------
#
# 三个动作都只动「技能目录」，不碰智能体分配：
# * 导出是把源目录打成 zip（只读）；
# * 导入是把一个 zip 解压进「当前用户的技能库」，然后重扫收录；
# * 复制是把一个已登记技能的目录拷到「当前用户」另一个内核的技能库，再重扫收录。
# 导入 / 复制后的新技能仍要「智能体分配」才在运行时生效（与市场安装同一套规则）。

#: 导入 / 复制时单个技能允许的体积上限（与技能市场下载对齐）
MAX_TRANSFER_BYTES = MAX_ARCHIVE_BYTES


def import_root_for_type(type_id: str, owner_id: str | None) -> Path:
    """导入 / 复制技能时落到哪里：当前用户在该内核下的技能库目录。

    先校验内核确实声明了技能（未知内核 / 不支持技能的内核直接报错），
    再返回平台托管的用户库路径 —— 不再写进内核的全局技能目录，保证按用户隔离。
    """
    skill_roots_for_type(type_id)
    return library_root(owner_id, type_id)


async def _same_slug_conflict(
    session: AsyncSession,
    *,
    type_id: str,
    slug: str,
    owner_id: str | None,
    new_id: str,
) -> bool:
    """导入 / 复制前检查「同内核 + 同 slug」是否已被共享技能或自己的技能占用。

    占用时拒绝：同一个内核下两份同名技能，物化到隔离目录会互相覆盖，调用名也会撞车。
    """
    statement = select(Skill.id).where(
        Skill.type_id == type_id,
        Skill.slug == slug,
        Skill.available == True,  # noqa: E712
    )
    if owner_id is None:
        statement = statement.where(Skill.owner_id.is_(None))
    else:
        statement = statement.where(
            or_(Skill.owner_id.is_(None), Skill.owner_id == owner_id)
        )
    return any(skill_id != new_id for (skill_id,) in (await session.execute(statement)).all())


def _type_label(type_id: str) -> str:
    try:
        return get_agent(type_id).name
    except KeyError:
        return type_id


def _name_taken(type_id: str, slug: str, action: str) -> SkillError:
    """同名技能的报错（导入 / 复制共用，避免两边文案走样）。

    同名可能来自共享技能（用户删不掉），所以提示里给出「换目录名」这条路。
    """
    return SkillError(
        f"{_type_label(type_id)} 下已有同名技能「{slug}」，"
        f"请先删除同名技能或换个目录名后再{action}"
    )


def _safe_slug(text: str) -> str | None:
    """尽量把一段文字变成安全的技能目录名；中文等无法直接使用时返回 ``None``。"""
    raw = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in "._-")) else "-" for ch in (text or "")
    )
    raw = re.sub(r"-{2,}", "-", raw).strip("-._")
    return safe_skill_dirname(raw)


def _is_declared_skill_dir(row: Skill, target: Path) -> bool:
    """``target`` 是否是该技能允许被操作的目录（技能根内部，且不是根本身）。

    * 用户技能库里的技能只能落在「该用户 + 该内核」的库目录内部；
    * 共享技能只能落在适配器声明过的技能根内部。

    导出 / 复制 / 删除前用它挡掉「数据库里的路径被改坏」的情况。
    """
    if is_library_row(row):
        bases = [library_root(row.owner_id, row.type_id)]
    else:
        bases = [Path(root.path) for root in skill_roots_for_type(row.type_id)]
    for base in bases:
        try:
            if target.resolve() == base.resolve():
                continue
        except OSError:
            continue
        if _is_under(base, target):
            return True
    return False


def _archive_top_dir(archive: bytes) -> str | None:
    """压缩包是否「恰好一层顶层目录」；是则返回该目录名，否则 ``None``。"""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            tops: set[str] = set()
            nested = False
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = [part for part in Path(info.filename).parts if part not in ("", ".")]
                if not parts:
                    continue
                tops.add(parts[0])
                if len(parts) > 1:
                    nested = True
    except (zipfile.BadZipFile, OSError):
        return None
    if len(tops) == 1 and nested:
        return next(iter(tops))
    return None


def export_skill_archive(row: Skill) -> bytes:
    """把一个技能的源目录打包成 zip（顶层目录名用技能 slug）。

    只读导出：不跟随符号链接、不改源目录；体积超过上限直接报错，
    免得生成一个自己都导不回来的压缩包。
    """
    base = Path(row.path)
    if not base.is_dir():
        raise SkillError(f"「{row.name}」的目录已不存在，无法导出")
    if not (base / SKILL_FILE).is_file():
        raise SkillError(f"「{row.name}」目录内没有 {SKILL_FILE}，无法导出")
    # 与删除同一道护栏：数据库里的路径即便被改坏，也只能碰内核声明过的技能根内部。
    if not _is_declared_skill_dir(row, base):
        raise SkillError(f"「{row.name}」不在它所属的技能目录内，拒绝导出")
    top = safe_skill_dirname(row.slug) or "skill"
    buffer = io.BytesIO()
    total = 0
    try:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                current = Path(dirpath)
                dirnames[:] = [
                    name
                    for name in sorted(dirnames)
                    if name not in SKIP_DIRS and not (current / name).is_symlink()
                ]
                for filename in sorted(filenames):
                    path = current / filename
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    total += size
                    if total > MAX_TRANSFER_BYTES:
                        raise SkillError(
                            f"「{row.name}」超过单个技能 {MAX_TRANSFER_BYTES // 1024 // 1024} MB 的导出上限"
                        )
                    zf.write(path, arcname=f"{top}/{path.relative_to(base).as_posix()}")
    except OSError as exc:
        raise SkillError(f"打包「{row.name}」失败：{exc}") from exc
    return buffer.getvalue()


def _find_extracted_skill_dir(payload: Path) -> Path:
    """在解压结果里找到技能目录：根目录本身就是技能，或恰好一个子目录是技能。"""
    if (payload / SKILL_FILE).is_file():
        return payload
    candidates = [
        child
        for child in sorted(payload.iterdir())
        if child.is_dir() and (child / SKILL_FILE).is_file()
    ]
    if not candidates:
        raise SkillError(f"压缩包里没有 {SKILL_FILE}，不是可导入的技能包")
    if len(candidates) > 1:
        raise SkillError("压缩包里有多份技能（多个 SKILL.md），请一次只导入一个")
    return candidates[0]


async def import_skill_archive(
    session: AsyncSession,
    *,
    type_id: str,
    archive: bytes,
    owner_id: str | None = None,
    filename: str | None = None,
) -> dict[str, str]:
    """把一个技能压缩包导入**当前用户**的技能库，并同步进统一编目。

    返回 ``{"skill_id", "name", "slug", "type_id"}``；失败抛 :class:`SkillError`。
    同名技能不会覆盖（避免悄悄改掉正在被调用的技能）：先删掉旧的再导入。
    """
    if not archive:
        raise SkillError("压缩包内容为空")
    if len(archive) > MAX_TRANSFER_BYTES:
        raise SkillError(
            f"压缩包超过 {MAX_TRANSFER_BYTES // 1024 // 1024} MB，已拒绝导入"
        )
    base = import_root_for_type(type_id, owner_id)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillError(f"无法创建技能目录 {base}：{exc}") from None

    staging = Path(tempfile.mkdtemp(prefix="arkswarm-skill-import-"))
    try:
        payload = staging / "payload"
        try:
            extract_skill_archive(archive, payload)
        except SkillHubError as exc:
            raise SkillError(f"导入失败：{exc}") from None
        skill_dir = _find_extracted_skill_dir(payload)

        # 目录名优先级：压缩包里的顶层技能目录名 → 上传文件名 → 兜底名。
        # 目录名就是内核调用技能时用的名字，不能带路径分隔符或非 ASCII 字符。
        # 解压时单层顶层目录已被剥掉，所以这里优先看压缩包原始结构。
        slug = (
            _safe_slug(_archive_top_dir(archive) or "")
            or _safe_slug(skill_dir.name if skill_dir != payload else "")
            or _safe_slug(Path(filename or "").stem)
            or "imported-skill"
        )
        target = base / slug
        new_id = library_skill_id(type_id, slug, owner_id)
        if target.exists() or await _same_slug_conflict(
            session, type_id=type_id, slug=slug, owner_id=owner_id, new_id=new_id
        ):
            raise _name_taken(type_id, slug, "导入")
        # 导入的是「用户技能」：市场安装标记不应跟着搬过来，否则会被误认成市场安装。
        (skill_dir / MARKET_MARKER_FILE).unlink(missing_ok=True)
        try:
            # copy 而不是 move：copytree 在目标已存在时直接报错，不会把技能塞进别人的目录里。
            shutil.copytree(skill_dir, target, symlinks=False)
        except FileExistsError:
            raise _name_taken(type_id, slug, "导入") from None
        except OSError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise SkillError(f"导入失败：无法写入技能目录（{exc}）") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    await rescan(session)
    row = await wait_for_market_registration(session, new_id)
    if row is None:
        raise SkillError(
            f"技能已导入到 {target}，但编目未收录；请到「技能」页点一次「重新扫描」"
        )
    return {"skill_id": row.id, "name": row.name, "slug": row.slug, "type_id": type_id}


def _ignore_copy_symlinks(directory: str, names: list[str]) -> set[str]:
    """复制技能时跳过符号链接与市场安装标记（不把链接指向的外部文件带进目标内核）。"""
    base = Path(directory)
    ignored = {name for name in names if (base / name).is_symlink()}
    if MARKET_MARKER_FILE in names:
        ignored.add(MARKET_MARKER_FILE)
    return ignored


async def copy_skill_to_kernel(
    session: AsyncSession, *, skill_id: str, type_id: str, owner_id: str | None = None
) -> dict[str, str]:
    """把一个已登记技能的目录复制到**当前用户**在另一个内核下的技能库。

    源可以是共享技能（复制后成为自己的个人技能），也可以是自己库里的技能；
    只复制文件，不复制智能体分配。同名技能不覆盖。
    """
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    if row.type_id == type_id:
        raise SkillError("目标内核与技能所属内核相同，无需复制")
    base = import_root_for_type(type_id, owner_id)
    source = Path(row.path)
    if not source.is_dir() or not (source / SKILL_FILE).is_file():
        raise SkillError(f"「{row.name}」的源目录已不存在，无法复制")
    if not _is_declared_skill_dir(row, source):
        raise SkillError(f"「{row.name}」不在它所属的技能目录内，拒绝复制")
    slug = safe_skill_dirname(row.slug)
    if slug is None:
        raise SkillError(f"「{row.name}」的目录名不安全，无法复制")

    target = base / slug
    new_id = library_skill_id(type_id, slug, owner_id)
    if target.exists() or await _same_slug_conflict(
        session, type_id=type_id, slug=slug, owner_id=owner_id, new_id=new_id
    ):
        raise _name_taken(type_id, slug, "复制")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SkillError(f"无法创建技能目录 {base}：{exc}") from None
    try:
        shutil.copytree(source, target, symlinks=False, ignore=_ignore_copy_symlinks)
    except FileExistsError:
        # 检查与复制之间目标被另一个请求建出来了：不能把别人的目录删掉。
        raise _name_taken(type_id, slug, "复制") from None
    except OSError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise SkillError(f"复制「{row.name}」失败：{exc}") from None

    await rescan(session)
    new_row = await wait_for_market_registration(session, new_id)
    if new_row is None:
        raise SkillError(
            f"技能已复制到 {target}，但编目未收录；请到「技能」页点一次「重新扫描」"
        )
    return {
        "skill_id": new_row.id,
        "name": new_row.name,
        "slug": new_row.slug,
        "type_id": type_id,
    }
