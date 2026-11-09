from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..agents import all_agents, get_agent

router = APIRouter(tags=["agent-types"])


@router.get("/agent-types")
async def list_agent_types() -> dict:
    return {"items": [agent.descriptor() for agent in all_agents()]}


@router.get("/agent-types/{type_id}")
async def get_agent_type(type_id: str) -> dict:
    try:
        return get_agent(type_id).descriptor()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
