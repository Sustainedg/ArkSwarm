"""智能体接口：人格 + 内核 + 网关 + 模型 + 技能分配。

智能体是用户配置与使用的最小单位（原「角色」与「智能体实例」合并而来），
它引用一个网关（密钥的唯一来源）与一个内核适配器（``type_id``）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, get_agent as get_definition
from ..agents.base import Availability
from ..credentials import CredentialError
from ..credentials import reads as credential_reads
from ..db import get_session
from ..models import AgentCredential, AgentProfile, AgentSkill, Conversation, Gateway, Skill
from ..runtime import availability as availability_service
from ..runtime import credentials as credential_service
from ..runtime import skills as skill_service
from ..runtime.gateway import bind_gateway
from ..schemas import (
    AgentCredentialBrief,
    AgentProfileCreate,
    AgentProfileRead,
    AgentProfileUpdate,
    AgentSkillBrief,
    AgentSkillsUpdate,
    AgentTestResult,
)

router = APIRouter(prefix="/agents", tags=["agents"])


def _availability(profile: AgentProfile, statuses: dict[str, Availability]) -> Availability | None:
    try:
        get_definition(profile.type_id)
    except KeyError:
        return None
    return statuses.get(profile.type_id)


async def _gateway_for(profile: AgentProfile, session: AsyncSession) -> Gateway | None:
    if not profile.gateway_id:
        return None
    return await session.get(Gateway, profile.gateway_id)


async def _skill_briefs(profile_id: str, session: AsyncSession) -> list[AgentSkillBrief]:
    links = (
        await session.execute(
            select(AgentSkill).where(AgentSkill.agent_profile_id == profile_id).order_by(AgentSkill.position)
        )
    ).scalars().all()
    if not links:
        return []
    ids = [link.skill_id for link in links]
    rows = {
        row.id: row
        for row in (await session.execute(select(Skill).where(Skill.id.in_(ids)))).scalars().all()
    }
    briefs: list[AgentSkillBrief] = []
    for skill_id in ids:
        row = rows.get(skill_id)
        if row is None:
            continue
        briefs.append(
            AgentSkillBrief(
                id=row.id,
                name=row.name,
                type_id=row.type_id,
                source_kind=row.source_kind,
                available=row.available,
            )
        )
    return briefs


async def _credential_briefs(profile_id: str, session: AsyncSession) -> list[AgentCredentialBrief]:
    """智能体引用到的凭据摘要（只有名字、说明与掩码）。"""
    rows = await credential_service.linked_rows(session, profile_id)
    return [AgentCredentialBrief(**item) for item in credential_reads(rows)]


async def _to_read(
    profile: AgentProfile,
    session: AsyncSession,
    availability: Availability | None = None,
) -> AgentProfileRead:
    type_name = runtime = None
    try:
        definition = get_definition(profile.type_id)
        type_name, runtime = definition.name, definition.runtime.value
    except KeyError:
        availability = None
    gateway = await _gateway_for(profile, session)
    briefs = await _skill_briefs(profile.id, session)
    credentials = await _credential_briefs(profile.id, session)
    return AgentProfileRead(
        id=profile.id,
        name=profile.name,
        avatar=profile.avatar,
        tagline=profile.tagline,
        description=profile.description,
        system_prompt=profile.system_prompt,
        type_id=profile.type_id,
        gateway_id=profile.gateway_id,
        model=profile.model,
        options=profile.options or {},
        enabled=profile.enabled,
        is_default=profile.is_default,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        type_name=type_name,
        runtime=runtime,
        availability=availability.to_dict() if availability else None,
        gateway_name=gateway.name if gateway else None,
        gateway_base_url=gateway.base_url if gateway else None,
        gateway_kind=gateway.kind if gateway else None,
        gateway_api_key_set=bool(gateway and gateway.api_key_encrypted),
        credentials=credentials,
        skill_ids=[brief.id for brief in briefs],
        skills=briefs,
    )


async def _has_default(session: AsyncSession) -> bool:
    row = (
        await session.execute(
            select(AgentProfile.id)
            .where(AgentProfile.is_default == True, AgentProfile.deleted_at.is_(None))  # noqa: E712
            .limit(1)
        )
    ).first()
    return row is not None


async def _clear_other_defaults(keep_id: str, session: AsyncSession) -> None:
    """五角星全局只有一颗：点亮自己的同时把别人的摘掉。"""
    await session.execute(
        update(AgentProfile).where(AgentProfile.id != keep_id).values(is_default=False)
    )


async def _promote_default(session: AsyncSession) -> None:
    """默认智能体被删掉后补一颗星，免得「新建对话」突然没得用。"""
    if await _has_default(session):
        return
    nxt = (
        await session.execute(
            select(AgentProfile)
            .where(AgentProfile.deleted_at.is_(None))
            .order_by(AgentProfile.enabled.desc(), AgentProfile.created_at)
            .limit(1)
        )
    ).scalars().first()
    if nxt is None:
        return
    nxt.is_default = True
    session.add(nxt)


async def _active_profile(profile_id: str, session: AsyncSession) -> AgentProfile:
    """取一个**未删除**的智能体；软删除过的一律按不存在处理。"""
    profile = await session.get(AgentProfile, profile_id)
    if profile is None or profile.deleted_at is not None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return profile


async def _require_gateway(gateway_id: str | None, session: AsyncSession) -> Gateway | None:
    if not gateway_id:
        return None
    gateway = await session.get(Gateway, gateway_id)
    if gateway is None or gateway.deleted_at is not None:
        raise HTTPException(status_code=422, detail="指定的网关不存在")
    return gateway


@router.get("")
async def list_agents(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        await session.execute(
            select(AgentProfile)
            .where(AgentProfile.deleted_at.is_(None))
            .order_by(AgentProfile.created_at)
        )
    ).scalars().all()
    # 可用性按 type_id 批量取一次，避免「每行都探测一遍」。
    statuses = await availability_service.for_types(row.type_id for row in rows)
    return {"items": [(await _to_read(row, session, statuses.get(row.type_id))).model_dump() for row in rows]}


@router.post("", status_code=201)
async def create_agent(payload: AgentProfileCreate, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        definition = get_definition(payload.type_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await _require_gateway(payload.gateway_id, session)
    profile = AgentProfile(
        name=payload.name,
        type_id=payload.type_id,
        avatar=payload.avatar or "🛠️",
        tagline=payload.tagline,
        description=payload.description,
        system_prompt=payload.system_prompt or "",
        gateway_id=payload.gateway_id,
        model=payload.model,
        options=definition.coerce_options(payload.options or {}),
        enabled=payload.enabled,
        # 第一个智能体自动戴上五角星（新建对话就能直接聊）；显式声明时以显式声明为准
        is_default=bool(payload.is_default) or not await _has_default(session),
    )
    session.add(profile)
    if profile.is_default:
        await _clear_other_defaults(profile.id, session)
    await session.commit()
    await session.refresh(profile)
    if payload.credentials:
        try:
            await credential_service.set_agent_credentials(session, profile, payload.credentials)
        except CredentialError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.get("/{profile_id}")
async def get_agent(profile_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    profile = await _active_profile(profile_id, session)
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.patch("/{profile_id}")
async def update_agent(
    profile_id: str, payload: AgentProfileUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    profile = await _active_profile(profile_id, session)
    data = payload.model_dump(exclude_unset=True)
    if data.get("type_id"):
        try:
            get_definition(data["type_id"])
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    if "gateway_id" in data:
        await _require_gateway(data["gateway_id"], session)
    credentials: list = data.pop("credentials", None) or []
    if data.get("is_default") is not None:
        # 点星 / 取消星：点亮时把别人的摘掉（全局唯一）
        flag = bool(data.pop("is_default"))
        profile.is_default = flag
        if flag:
            await _clear_other_defaults(profile.id, session)
    if "options" in data and data["options"] is not None:
        type_id = data.get("type_id") or profile.type_id
        try:
            data["options"] = get_definition(type_id).coerce_options(data["options"])
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    for field, value in data.items():
        if value is not None or field in {"gateway_id", "model", "tagline", "description"}:
            setattr(profile, field, value)
    if data.get("type_id"):
        # 换内核后，原来分配的技能可能不属于新型号，直接丢弃并在响应里体现。
        await _drop_incompatible_skills(profile, session)
    profile.updated_at = datetime.now(timezone.utc)
    session.add(profile)
    await session.commit()
    if "credentials" in payload.model_fields_set:
        try:
            await credential_service.set_agent_credentials(session, profile, credentials)
        except CredentialError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    await session.refresh(profile)
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.delete("/{profile_id}", status_code=204)
async def delete_agent(profile_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    """删除智能体（软删除）。

    智能体常被历史引用（对话外键是 ``RESTRICT``），硬删会连历史一起打断。
    因此统一打 ``deleted_at``：它从所有可选列表里消失，而历史对话仍能显示当时的
    名字与头像、也仍可继续对话。
    """
    profile = await _active_profile(profile_id, session)
    profile.deleted_at = datetime.now(timezone.utc)
    profile.updated_at = profile.deleted_at
    profile.is_default = False
    session.add(profile)
    # 删掉的正是那颗星时，从剩下的里补一颗（还有智能体就永远有一颗）
    await _promote_default(session)
    await session.commit()
    return Response(status_code=204)


async def _drop_incompatible_skills(profile: AgentProfile, session: AsyncSession) -> None:
    for link in (
        await session.execute(
            select(AgentSkill).where(AgentSkill.agent_profile_id == profile.id)
        )
    ).scalars().all():
        row = await session.get(Skill, link.skill_id)
        if row is None or row.type_id != profile.type_id:
            await session.delete(link)
    await session.flush()


@router.put("/{profile_id}/skills")
async def set_agent_skills(
    profile_id: str, payload: AgentSkillsUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    """整表替换智能体分配到的技能（界面已禁止选错类型，这里是服务端兜底）。"""
    profile = await _active_profile(profile_id, session)
    try:
        await skill_service.set_agent_skills(session, profile, payload.skill_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    statuses = await availability_service.for_types([profile.type_id])
    return (await _to_read(profile, session, statuses.get(profile.type_id))).model_dump()


@router.post("/{profile_id}/duplicate", status_code=201)
async def duplicate_agent(profile_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    profile = await _active_profile(profile_id, session)
    clone = AgentProfile(
        name=f"{profile.name} 副本",
        type_id=profile.type_id,
        avatar=profile.avatar,
        tagline=profile.tagline,
        description=profile.description,
        system_prompt=profile.system_prompt,
        gateway_id=profile.gateway_id,
        model=profile.model,
        options=dict(profile.options or {}),
        enabled=profile.enabled,
    )
    session.add(clone)
    await session.commit()
    await session.refresh(clone)
    links = (
        await session.execute(
            select(AgentSkill)
            .where(AgentSkill.agent_profile_id == profile.id)
            .order_by(AgentSkill.position)
        )
    ).scalars().all()
    for link in links:
        session.add(
            AgentSkill(agent_profile_id=clone.id, skill_id=link.skill_id, position=link.position)
        )
    for link in (
        await session.execute(
            select(AgentCredential)
            .where(AgentCredential.agent_profile_id == profile.id)
            .order_by(AgentCredential.position)
        )
    ).scalars().all():
        session.add(
            AgentCredential(
                agent_profile_id=clone.id, credential_id=link.credential_id, position=link.position
            )
        )
    await session.commit()
    statuses = await availability_service.for_types([clone.type_id])
    return (await _to_read(clone, session, statuses.get(clone.type_id))).model_dump()


@router.post("/{profile_id}/test")
async def test_agent(profile_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """连通性测试：实时探测运行环境与网关（用户主动触发，走线程池，不冻住事件循环）。"""
    profile = await _active_profile(profile_id, session)
    try:
        definition = get_definition(profile.type_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    gateway = await _gateway_for(profile, session)

    probed = await availability_service.probe(definition)
    checks = [
        {"name": "适配器", "ok": True, "detail": f"{definition.name}（{definition.runtime.value}）"},
        {
            "name": "运行环境",
            "ok": bool(probed and probed.installed),
            "detail": (probed or availability_service.cheap_availability(definition)).detail,
        },
        {
            "name": "网关",
            "ok": bool(gateway),
            "detail": f"{gateway.name}（{gateway.base_url}）" if gateway else "未选择网关",
        },
    ]
    binding = bind_gateway(definition, gateway)
    ctx = AgentContext(
        agent_id=profile.id,
        agent_name=profile.name,
        options=definition.coerce_options(profile.options or {}),
        gateway_url=binding.url,
        api_key=binding.api_key,
        auth_style=binding.auth_style,
        model=profile.model or definition.default_model,
        system_prompt=None,
        prompt="ping",
    )
    ok, message = await definition.probe(ctx)
    checks.append({"name": "连通性", "ok": ok, "detail": message})
    return AgentTestResult(ok=all(c["ok"] for c in checks), message=message, checks=checks).model_dump()

__all__ = ["router"]
