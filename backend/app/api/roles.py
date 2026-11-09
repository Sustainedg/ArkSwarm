from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..db import get_session
from ..models import AgentInstance, Conversation, Role
from ..schemas import RoleCreate, RoleRead, RoleUpdate

router = APIRouter(prefix="/roles", tags=["roles"])


async def _to_read(role: Role, session: AsyncSession) -> RoleRead:
    agent = await session.get(AgentInstance, role.agent_id)
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
    )


@router.get("")
async def list_roles(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Role).order_by(Role.created_at))).scalars().all()
    return {"items": [(await _to_read(r, session)).model_dump() for r in rows]}


@router.post("", status_code=201)
async def create_role(payload: RoleCreate, session: AsyncSession = Depends(get_session)) -> dict:
    agent = await session.get(AgentInstance, payload.agent_id)
    if agent is None:
        raise HTTPException(status_code=422, detail="agent_id does not exist")
    role = Role(**payload.model_dump())
    session.add(role)
    await session.commit()
    await session.refresh(role)
    return (await _to_read(role, session)).model_dump()


@router.get("/{role_id}")
async def get_role(role_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    return (await _to_read(role, session)).model_dump()


@router.patch("/{role_id}")
async def update_role(
    role_id: str, payload: RoleUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("agent_id") and await session.get(AgentInstance, data["agent_id"]) is None:
        raise HTTPException(status_code=422, detail="agent_id does not exist")
    for field, value in data.items():
        if value is not None:
            setattr(role, field, value)
    role.updated_at = datetime.now(timezone.utc)
    session.add(role)
    await session.commit()
    await session.refresh(role)
    return (await _to_read(role, session)).model_dump()


@router.delete("/{role_id}", status_code=204)
async def delete_role(role_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    in_use = (
        await session.execute(select(Conversation).where(Conversation.role_id == role_id))
    ).scalars().all()
    if in_use:
        raise HTTPException(status_code=409, detail=f"role is used by {len(in_use)} conversation(s)")
    await session.delete(role)
    await session.commit()
    return Response(status_code=204)


@router.post("/{role_id}/duplicate", status_code=201)
async def duplicate_role(role_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="role not found")
    clone = Role(
        name=f"{role.name} copy",
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
    return (await _to_read(clone, session)).model_dump()
