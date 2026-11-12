from fastapi import APIRouter

from ..collab.api import router as collab_router
from . import (
    agents,
    agent_types,
    conversations,
    credentials,
    gateways,
    mcp,
    runtimes,
    runs,
    skillhub,
    skills,
    system,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(system.router)
api_router.include_router(agent_types.router)
api_router.include_router(agents.router)
api_router.include_router(gateways.router)
api_router.include_router(credentials.router)
api_router.include_router(skills.router)
api_router.include_router(mcp.router)
api_router.include_router(skillhub.router)
api_router.include_router(runtimes.router)
api_router.include_router(conversations.router)
api_router.include_router(runs.router)
api_router.include_router(collab_router)

__all__ = ["api_router"]
