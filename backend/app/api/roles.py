from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import get_session
from ..models import AgentInstance, Role, RoleSkill, Skill
from ..runtime import skills as skill_service
from ..schemas import RoleCreate, RoleRead, RoleSkillBrief, RoleSkillsUpdate, RoleUpdate

router = APIRouter(prefix="/roles", tags=["roles"])


async def _active_role(role_id: str, session: AsyncSession) -> Role:
    """取一个**未删除**的角色；软删除过的角色一律按不存在处理。"""
    role = await session.get(Role, role_id)
    if role is None or role.deleted_at is not None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return role


async def _skill_briefs(role_id: str, session: AsyncSession) -> list[RoleSkillBrief]:
    statement = select(RoleSkill).where(RoleSkill.role_id == role_id).order_by(RoleSkill.position)
    links = (await session.execute(statement)).scalars().all()
    if not links:
        return []
    ids = [link.skill_id for link in links]
    rows = {
        row.id: row
        for row in (await session.execute(select(Skill).where(Skill.id.in_(ids)))).scalars().all()
    }
    briefs: list[RoleSkillBrief] = []
    for skill_id in ids:
        row = rows.get(skill_id)
        if row is None:
            continue
        briefs.append(
            RoleSkillBrief(
                id=row.id,
                name=row.name,
                type_id=row.type_id,
                source_kind=row.source_kind,
                available=row.available,
            )
        )
    return briefs


async def _to_read(role: Role, session: AsyncSession) -> RoleRead:
    agent = await session.get(AgentInstance, role.agent_id)
    briefs = await _skill_briefs(role.id, session)
    return RoleRead(
        id=role.id,
        name=role.name,
        avatar=role.avatar,
        tagline=role.tagline,
        system_prompt=role.system_prompt,
        agent_id=role.agent_id,
        workspace_dir=role.workspace_dir,
        parameters=role.parameters or {},
        created_at=role.created_at,
        updated_at=role.updated_at,
        agent_name=agent.name if agent else None,
        agent_type_id=agent.type_id if agent else None,
        skill_ids=[brief.id for brief in briefs],
        skills=briefs,
    )


@router.get("")
async def list_roles(session: AsyncSession = Depends(get_session)) -> dict:
    statement = select(Role).where(Role.deleted_at.is_(None)).order_by(Role.created_at)
    rows = (await session.execute(statement)).scalars().all()
    return {"items": [(await _to_read(r, session)).model_dump() for r in rows]}


@router.post("", status_code=201)
async def create_role(payload: RoleCreate, session: AsyncSession = Depends(get_session)) -> dict:
    agent = await session.get(AgentInstance, payload.agent_id)
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status_code=422, detail="指定的 agent_id 不存在")
    role = Role(**payload.model_dump())
    session.add(role)
    await session.commit()
    await session.refresh(role)
    return (await _to_read(role, session)).model_dump()


@router.get("/{role_id}")
async def get_role(role_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    role = await _active_role(role_id, session)
    return (await _to_read(role, session)).model_dump()


@router.patch("/{role_id}")
async def update_role(
    role_id: str, payload: RoleUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    role = await _active_role(role_id, session)
    data = payload.model_dump(exclude_unset=True)
    if data.get("agent_id"):
        agent = await session.get(AgentInstance, data["agent_id"])
        if agent is None or agent.deleted_at is not None:
            raise HTTPException(status_code=422, detail="指定的 agent_id 不存在")
    for field, value in data.items():
        if value is not None:
            setattr(role, field, value)
    if data.get("agent_id"):
        # 改绑智能体后，原来分配的技能可能与新型号不匹配，直接丢弃并在响应里体现。
        await _drop_incompatible_skills(role, session)
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    await session.commit()
    await session.refresh(role)
    return (await _to_read(role, session)).model_dump()


@router.delete("/{role_id}", status_code=204)
async def delete_role(role_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """删除角色。

    角色常被历史引用（对话、协作工单的外键都是 ``RESTRICT``），
    硬删会连历史一起打断。因此统一做**软删除**：打上 ``deleted_at`` 后
    角色从所有可选列表里消失，而历史对话仍能显示当时的角色名与头像、也仍可继续对话。
    """
    role = await _active_role(role_id, session)
    role.deleted_at = datetime.now(timezone.utc)
    role.updated_at = role.deleted_at
    session.add(role)
    await session.commit()
    return Response(status_code=204)


async def _drop_incompatible_skills(role: Role, session: AsyncSession) -> None:
    agent = await session.get(AgentInstance, role.agent_id)
    type_id = agent.type_id if agent else None
    for link in (
        await session.execute(select(RoleSkill).where(RoleSkill.role_id == role.id))
    ).scalars().all():
        row = await session.get(Skill, link.skill_id)
        if row is None or type_id is None or row.type_id != type_id:
            await session.delete(link)
    await session.flush()


@router.put("/{role_id}/skills")
async def set_role_skills(
    role_id: str, payload: RoleSkillsUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    """整表替换角色分配到的技能（界面已禁止选错类型，这里是服务端兜底）。"""
    role = await _active_role(role_id, session)
    agent = await session.get(AgentInstance, role.agent_id)
    if agent is None:
        raise HTTPException(status_code=422, detail="该角色绑定的智能体已不存在")
    try:
        await skill_service.set_role_skills(session, role, agent.type_id, payload.skill_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return (await _to_read(role, session)).model_dump()


@router.post("/{role_id}/duplicate", status_code=201)
async def duplicate_role(role_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    role = await _active_role(role_id, session)
    clone = Role(
        name=f"{role.name} 副本",
        avatar=role.avatar,
        tagline=role.tagline,
        system_prompt=role.system_prompt,
        agent_id=role.agent_id,
        workspace_dir=role.workspace_dir,
        parameters=dict(role.parameters or {}),
    )
    session.add(clone)
    await session.commit()
    await session.refresh(clone)
    statement = select(RoleSkill).where(RoleSkill.role_id == role.id).order_by(RoleSkill.position)
    for link in (await session.execute(statement)).scalars().all():
        session.add(RoleSkill(role_id=clone.id, skill_id=link.skill_id, position=link.position))
    await session.commit()
    return (await _to_read(clone, session)).model_dump()
