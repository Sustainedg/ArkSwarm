"""技能：统一编目、角色分配与运行时物化。

各智能体内核的技能目录由适配器的 :meth:`AgentDefinition.discover_skills` 只读扫描，
这里把它们汇成一份统一目录（``skills`` 表），并按角色的分配结果在运行时物化到
该内核的隔离目录，从而保证「角色能用的技能 == 分配到的技能」。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, AgentDefinition, all_agents
from ..agents.skills import (
    MAX_SKILL_BYTES,
    SKILL_FILE,
    SkillMount,
    SkillSpec,
    safe_skill_dirname,
)
from ..models import AgentInstance, Conversation, Role, RoleSkill, Skill
from ..settings import get_settings

logger = logging.getLogger("magenta")

#: 技能来源的中文标签
SOURCE_LABELS = {
    "user": "个人技能",
    "project": "项目技能",
    "plugin": "插件技能",
    "system": "内核系统技能",
    "manual": "平台技能",
}

#: 同名技能的去重优先级（越小越优先）
SOURCE_PRECEDENCE = {"manual": 0, "user": 1, "project": 2, "plugin": 3, "system": 4}

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

    目录消失的技能只标记 ``available=False``，不删除记录，避免角色分配断链。
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


async def usage_counts(session: AsyncSession) -> dict[str, int]:
    """每个技能被多少个角色使用（已软删除的角色不再算"使用中"）。"""
    statement = (
        select(RoleSkill.skill_id)
        .join(Role, Role.id == RoleSkill.role_id)
        .where(Role.deleted_at.is_(None))
    )
    counts: dict[str, int] = {}
    for skill_id in (await session.execute(statement)).scalars().all():
        counts[skill_id] = counts.get(skill_id, 0) + 1
    return counts


async def roles_using(session: AsyncSession, skill_id: str) -> list[Role]:
    statement = (
        select(Role)
        .join(RoleSkill, RoleSkill.role_id == Role.id)
        .where(RoleSkill.skill_id == skill_id, Role.deleted_at.is_(None))
        .order_by(Role.created_at)
    )
    return list((await session.execute(statement)).scalars().all())


def read_body(skill_path: str) -> tuple[str, bool]:
    """读取 SKILL.md 正文（供详情页展示）；返回 ``(正文, 是否被截断)``。"""
    try:
        target = Path(skill_path) / SKILL_FILE
        data = target.read_bytes()
    except OSError:
        return "", False
    truncated = len(data) > MAX_BODY_BYTES
    return data[:MAX_BODY_BYTES].decode("utf-8", "replace"), truncated


# --- 分配 -------------------------------------------------------------------


async def role_skill_ids(session: AsyncSession, role_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(RoleSkill).where(RoleSkill.role_id == role_id).order_by(RoleSkill.position)
        )
    ).scalars().all()
    return [row.skill_id for row in rows]


async def set_role_skills(
    session: AsyncSession, role: Role, agent_type_id: str, skill_ids: list[str]
) -> list[str]:
    """整表替换角色的技能分配；返回实际写入的 skill_id 列表。

    只接受「与角色绑定的智能体类型一致且可用」的技能：界面已经禁止选择不匹配的
    技能，这里是服务端兜底。
    """
    wanted = list(dict.fromkeys(skill_ids))
    for skill_id in wanted:
        row = await session.get(Skill, skill_id)
        if row is None:
            raise ValueError(f"技能不存在：{skill_id}")
        if row.type_id != agent_type_id:
            raise ValueError(
                f"技能「{row.name}」属于 {row.type_id}，"
                f"与本角色绑定的智能体（{agent_type_id}）不匹配"
            )

    for link in (
        await session.execute(select(RoleSkill).where(RoleSkill.role_id == role.id))
    ).scalars().all():
        await session.delete(link)
    await session.flush()

    for position, skill_id in enumerate(wanted):
        session.add(RoleSkill(role_id=role.id, skill_id=skill_id, position=position))
    await session.commit()
    return wanted


async def skills_for_role(session: AsyncSession, role: Role, agent_type_id: str) -> list[SkillSpec]:
    """角色在本轮运行中真正生效的技能（只取同类型且目录仍在的）。"""
    ids = await role_skill_ids(session, role.id)
    if not ids:
        return []
    rows = (await session.execute(select(Skill).where(Skill.id.in_(ids)))).scalars().all()
    by_id = {row.id: row for row in rows}
    specs: list[SkillSpec] = []
    for skill_id in ids:
        row = by_id.get(skill_id)
        if row is None or not row.available or row.type_id != agent_type_id:
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
    )


# --- 物化 -------------------------------------------------------------------


async def prepare_skill_home(
    session: AsyncSession,
    definition: AgentDefinition,
    agent: AgentInstance,
    role: Role,
    conversation: Conversation,
    ctx: AgentContext,
) -> list[str]:
    """把角色分配到的技能物化进该内核的隔离目录，并把结果写回 ``ctx``。

    返回需要提示给用户的说明/告警（由运行层作为 ``log`` 事件暴露）。
    """
    ctx.skills = await skills_for_role(session, role, agent.type_id)
    mount = definition.skill_mount(ctx)
    if mount is None:
        return []
    home = get_settings().agent_homes_root / conversation.id / mount.home_name
    first_run = not home.exists()
    warnings = materialize(home, mount, ctx.skills)
    ctx.skill_home_dir = str(home)
    if first_run and ctx.session_id:
        # 隔离目录是刚建的：内核侧没有这条对话的历史记录，续接旧会话必然失败
        # （旧记录还在原来的配置目录里）。这里改为一轮新会话，并明确告知用户。
        ctx.session_id = None
        warnings.append(
            "内核隔离目录里还没有这条对话的历史会话，本轮已改为新会话"
            "（旧会话记录仍在原配置目录中，如需保留请到「对话」页操作）"
        )
    return [*mount.notes, *warnings]


def materialize(home: Path, mount: SkillMount, specs: Iterable[SkillSpec]) -> list[str]:
    """按物化计划把技能目录与内核配置写进隔离目录；返回告警列表。"""
    warnings: list[str] = []
    try:
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
    except OSError as exc:
        return [f"无法创建内核隔离目录 {home}：{exc}"]

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


def _safe_relative(relative: str) -> Path | None:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts:
        return None
    if any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate


def cleanup_conversation(conversation_id: str) -> None:
    """删除对话时清掉它的隔离配置目录（尽力而为）。"""
    home = get_settings().agent_homes_root / conversation_id
    shutil.rmtree(home, ignore_errors=True)
