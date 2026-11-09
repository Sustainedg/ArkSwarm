from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentContext, get_agent
from ..db import get_session
from ..models import AgentInstance, Role
from ..schemas import AgentCreate, AgentRead, AgentTestResult, AgentUpdate
from ..security import decrypt, encrypt, mask

router = APIRouter(prefix="/agents", tags=["agents"])


def to_read(agent: AgentInstance) -> AgentRead:
    secret = decrypt(agent.api_key_encrypted)
    gateway_url, model = agent.gateway_url, agent.model
    try:
        definition = get_agent(agent.type_id)
        # 未被用户显式修改的网关/模型，回落到适配器读到的本机真实配置。
        gateway_url = definition.resolve_gateway(agent.gateway_url)
        model = definition.resolve_model(agent.model)
        type_name, runtime, availability = definition.name, definition.runtime.value, definition.availability().to_dict()
    except KeyError:
        type_name, runtime, availability = None, None, None
    return AgentRead(
        id=agent.id,
        name=agent.name,
        type_id=agent.type_id,
        description=agent.description,
        gateway_url=gateway_url,
        model=model,
        options=agent.options or {},
        enabled=agent.enabled,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        type_name=type_name,
        runtime=runtime,
        api_key_set=bool(agent.api_key_encrypted),
        api_key_preview=mask(secret),
        availability=availability,
    )


def _coerce(definition, options: dict | None) -> dict:
    return definition.coerce_options(options or {})


@router.get("")
async def list_agents(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(AgentInstance).order_by(AgentInstance.created_at))).scalars().all()
    return {"items": [to_read(a).model_dump() for a in rows]}


@router.post("", status_code=201)
async def create_agent(payload: AgentCreate, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        definition = get_agent(payload.type_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    defaults = definition.system_defaults()
    agent = AgentInstance(
        name=payload.name,
        type_id=payload.type_id,
        description=payload.description,
        gateway_url=payload.gateway_url or defaults.get("gateway_url") or definition.default_gateway,
        model=payload.model or defaults.get("model") or definition.default_model,
        api_key_encrypted=encrypt(payload.api_key),
        options=_coerce(definition, payload.options),
        enabled=payload.enabled,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return to_read(agent).model_dump()


@router.get("/{agent_id}")
async def get_agent_instance(agent_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    agent = await session.get(AgentInstance, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    return to_read(agent).model_dump()


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str, payload: AgentUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    agent = await session.get(AgentInstance, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")

    data = payload.model_dump(exclude_unset=True)
    if "api_key" in data:
        secret = data.pop("api_key")
        agent.api_key_encrypted = encrypt(secret) if secret else None
    if "options" in data and data["options"] is not None:
        try:
            agent.options = _coerce(get_agent(agent.type_id), data.pop("options"))
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    for field, value in data.items():
        if value is not None:
            setattr(agent, field, value)
    agent.updated_at = datetime.now(timezone.utc)
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return to_read(agent).model_dump()


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, session: AsyncSession = Depends(get_session)) -> Response:
    agent = await session.get(AgentInstance, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    in_use = (await session.execute(select(Role).where(Role.agent_id == agent_id))).scalars().all()
    if in_use:
        names = ", ".join(r.name for r in in_use)
        raise HTTPException(status_code=409, detail=f"该智能体正被以下角色使用：{names}")
    await session.delete(agent)
    await session.commit()
    return Response(status_code=204)


@router.post("/{agent_id}/test")
async def test_agent(agent_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    agent = await session.get(AgentInstance, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    try:
        definition = get_agent(agent.type_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    availability = definition.availability()
    checks = [
        {"name": "适配器", "ok": True, "detail": f"{definition.name}（{definition.runtime.value}）"},
        {"name": "运行环境", "ok": availability.installed, "detail": availability.detail},
    ]

    ctx = AgentContext(
        agent_id=agent.id,
        agent_name=agent.name,
        options=definition.coerce_options(agent.options or {}),
        gateway_url=definition.resolve_gateway(agent.gateway_url),
        api_key=decrypt(agent.api_key_encrypted),
        model=definition.resolve_model(agent.model),
        system_prompt=None,
        prompt="ping",
    )
    ok, message = await definition.probe(ctx)
    checks.append({"name": "连通性", "ok": ok, "detail": message})
    return AgentTestResult(ok=all(c["ok"] for c in checks), message=message, checks=checks).model_dump()


@router.post("/{agent_id}/duplicate", status_code=201)
async def duplicate_agent(agent_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    agent = await session.get(AgentInstance, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    clone = AgentInstance(
        name=f"{agent.name} 副本",
        type_id=agent.type_id,
        description=agent.description,
        gateway_url=agent.gateway_url,
        model=agent.model,
        api_key_encrypted=agent.api_key_encrypted,
        options=dict(agent.options or {}),
        enabled=agent.enabled,
    )
    session.add(clone)
    await session.commit()
    await session.refresh(clone)
    return to_read(clone).model_dump()
