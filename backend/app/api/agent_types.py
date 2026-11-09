from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..agents import all_agents, get_agent
from ..security import mask

router = APIRouter(tags=["agent-types"])


def _describe(definition) -> dict:
    """在适配器描述之上补一份「本机真实配置」，供界面展示，密钥只给掩码。"""
    data = definition.descriptor()
    defaults = definition.system_defaults()
    api_key = defaults.get("api_key")
    data["system_defaults"] = {
        "gateway_url": defaults.get("gateway_url"),
        "model": defaults.get("model"),
        "api_key_set": bool(api_key),
        "api_key_preview": mask(api_key),
        "options": defaults.get("options") or {},
    }
    return data


@router.get("/agent-types")
async def list_agent_types() -> dict:
    return {"items": [_describe(agent) for agent in all_agents()]}


@router.get("/agent-types/{type_id}")
async def get_agent_type(type_id: str) -> dict:
    try:
        return _describe(get_agent(type_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
