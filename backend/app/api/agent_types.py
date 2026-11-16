from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ..agents import AgentDefinition, all_agents, get_agent
from ..agents.base import Availability
from ..models import User
from ..runtime import availability as availability_service
from ..security import mask
from .auth import current_user

router = APIRouter(tags=["agent-types"])


def _describe(definition: AgentDefinition, availability: Availability) -> dict:
    """在适配器描述之上补一份「本机真实配置」，供界面展示，密钥只给掩码。

    ``availability`` 是框架缓存的检测结果（见 ``runtime/availability.py``）；这里传进去
    而不是让 ``descriptor()`` 现探，请求路径才不会去起 ``内核 --version`` 子进程。
    """
    data = definition.descriptor(availability)
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
async def list_agent_types(
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    definitions = all_agents()
    statuses = await availability_service.statuses(definitions)
    return {"items": [_describe(definition, statuses[definition.id]) for definition in definitions]}


@router.get("/agent-types/{type_id}")
async def get_agent_type(
    type_id: str, user: Annotated[User, Depends(current_user)] = None
) -> dict:
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    statuses = await availability_service.for_types([definition.id])
    return _describe(definition, statuses[definition.id])
