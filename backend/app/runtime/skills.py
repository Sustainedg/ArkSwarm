"""技能：统一编目、智能体分配与运行时物化。

各智能体内核的技能目录由适配器的 :meth:`AgentDefinition.discover_skills` 只读扫描，
这里把它们汇成一份统一目录（``skills`` 表），并按智能体的分配结果在运行时物化到
该内核的隔离目录，从而保证「智能体能用的技能 == 分配到的技能」。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, all_agents, get_agent
from ..agents.skills import (
    MARKET_SOURCE,
    MAX_SKILL_BYTES,
    SKILL_FILE,
    MarketInstallInfo,
    SkillMount,
    SkillRoot,
    SkillSpec,
    read_market_marker,
    safe_skill_dirname,
    write_market_marker,
)
from ..models import AgentProfile, AgentSkill, Skill
from ..settings import get_settings
from ..skillhub import extract_skill_archive
from .isolation import ensure_home, home_for, note_fresh_home, safe_relative

logger = logging.getLogger("magenta")

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


# --- 编目 -------------------------------------------------------------------


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


async def rescan(session: AsyncSession) -> dict[str, int]:
    """重新扫描所有内核的技能目录，并同步到 ``skills`` 表。

    目录消失的技能只标记 ``available=False``，不删除记录，避免智能体分配断链。
    """
    discovered = discover_catalog()
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
) -> list[Skill]:
    statement = select(Skill).order_by(Skill.type_id, Skill.source_kind, Skill.name)
    if type_id:
        statement = statement.where(Skill.type_id == type_id)
    if source_kind:
        statement = statement.where(Skill.source_kind == source_kind)
    if available is not None:
        statement = statement.where(Skill.available == available)  # noqa: E712
    else:
        # 市场技能被卸载后目录就没了，它不该再出现在技能列表里（用户看到的「已卸载」）。
        # 内核自带技能目录消失时仍保留一行并显示「目录已消失」，提示分配关系需要清理。
        statement = statement.where(
            or_(Skill.source_kind != MARKET_SOURCE, Skill.available == True)  # noqa: E712
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


def is_deletable(row: Skill) -> bool:
    """内核自带（``system``）之外的技能都允许从技能页删除。"""
    return row.source_kind != "system"


async def usage_counts(session: AsyncSession) -> dict[str, int]:
    """每个技能被多少个智能体使用（已软删除的智能体不再算"使用中"）。"""
    statement = (
        select(AgentSkill.skill_id)
        .join(AgentProfile, AgentProfile.id == AgentSkill.agent_profile_id)
        .where(AgentProfile.deleted_at.is_(None))
    )
    counts: dict[str, int] = {}
    for skill_id in (await session.execute(statement)).scalars().all():
        counts[skill_id] = counts.get(skill_id, 0) + 1
    return counts


async def agents_using(session: AsyncSession, skill_id: str) -> list[AgentProfile]:
    statement = (
        select(AgentProfile)
        .join(AgentSkill, AgentSkill.agent_profile_id == AgentProfile.id)
        .where(AgentSkill.skill_id == skill_id, AgentProfile.deleted_at.is_(None))
        .order_by(AgentProfile.created_at)
    )
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
        specs.append(spec_from_row(row))
    return specs


def spec_from_row(row: Skill) -> SkillSpec:
    return SkillSpec(
        type_id=row.type_id,
        slug=row.slug,
        name=row.name,
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
        try:
            shutil.copytree(source, skills_root / name, symlinks=False, dirs_exist_ok=True)
        except OSError as exc:
            warnings.append(f"技能「{spec.name}」复制失败：{exc}")

    for source_path, dest_rel in mount.carry:
        _carry_file(home, source_path, dest_rel, warnings)

    for dest_rel, patch in mount.patch.items():
        _patch_json(home / dest_rel, patch, warnings)

    return warnings


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


def install_root_for_type(type_id: str) -> SkillRoot:
    """某个内核用来安装「市场技能」的技能根（`installable` 中最靠前的一个）。"""
    roots = skill_roots_for_type(type_id)
    for root in roots:
        if root.installable:
            return root
    raise SkillError(f"该内核（{type_id}）没有可写入的技能目录，无法安装技能")


def market_install_root(type_id: str) -> str:
    """市场技能要落到的目录（按内核声明的技能根解析；找不到时按 ``<可写根>/.skillhub``）。"""
    roots = skill_roots_for_type(type_id)
    for root in roots:
        if root.source_kind == MARKET_SOURCE and root.installable:
            return root.path
    base = install_root_for_type(type_id).path
    return str(Path(base) / ".skillhub")


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
    if not any(
        root.installable and _is_under(Path(root.path), target)
        for root in skill_roots_for_type(type_id)
    ):
        raise SkillError(f"「{row.name}」不在可写入的技能目录内，拒绝删除")
    _delete_tree(target, row.name)


def _remove_skill_directory(type_id: str, row: Skill) -> None:
    """删掉技能自己的源目录；只允许删该内核声明过的技能根之下的目录。

    这是技能页「删除」的磁盘侧护栏：数据库里的路径即便被改坏，也只能删到技能根内部，
    并且目标必须真的像一个技能目录（含 ``SKILL.md``、不是符号链接、不是技能根本身）。
    """
    target = Path(row.path)
    if not target.exists():
        return
    if target.is_symlink() or not target.is_dir():
        raise SkillError(f"「{row.name}」的路径不是一个技能目录，拒绝删除")
    if not (target / SKILL_FILE).is_file():
        raise SkillError(f"「{row.name}」目录内没有 {SKILL_FILE}，拒绝删除")
    for root in skill_roots_for_type(type_id):
        base = Path(root.path)
        try:
            if target.resolve() == base.resolve():
                continue
        except OSError:
            continue
        if _is_under(base, target):
            _delete_tree(target, row.name)
            return
    raise SkillError(f"「{row.name}」不在内核声明的技能目录内，拒绝删除")


async def _drop_skill_row(session: AsyncSession, row: Skill) -> None:
    """断开智能体分配并删除编目行，然后重扫（目录已删时不会把技能加回来）。"""
    await session.execute(delete(AgentSkill).where(AgentSkill.skill_id == row.id))
    await session.delete(row)
    await session.commit()
    await rescan(session)


async def delete_skill(session: AsyncSession, skill_id: str) -> str:
    """删除一个非内核自带的技能（个人 / 项目 / 插件 / 市场…），返回技能名。"""
    row = await get_row(session, skill_id)
    if row is None:
        raise SkillNotFound(f"技能不存在：{skill_id}")
    if not is_deletable(row):
        raise SkillError(f"「{row.name}」是内核自带技能，不能删除")
    name = row.name
    _remove_skill_directory(row.type_id, row)
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
    source_url: str | None = None,
    display_slug: str | None = None,
) -> dict[str, Any]:
    """把一个市场技能的 zip 落盘到内核的原生技能根，并把它同步进统一编目。

    步骤：安全解压到 ``<market_root>/<slug>`` → 写入市场安装标记 → 触发一次重扫。
    返回 ``{"skill_id", "name", "warnings"}``；失败抛 :class:`SkillError`。
    """
    safe = safe_skill_dirname(slug)
    if safe is None:
        raise SkillError(f"技能标识不合法：{slug}")
    root = Path(market_install_root(type_id))
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
    skill_id = f"{type_id}:{safe}"
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
