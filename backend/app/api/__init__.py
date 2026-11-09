from fastapi import APIRouter

from . import agents, agent_types, conversations, roles, runs, system

api_router = APIRouter(prefix="/api")
api_router.include_router(system.router)
api_router.include_router(agent_types.router)
api_router.include_router(agents.router)
api_router.include_router(roles.router)
api_router.include_router(conversations.router)
api_router.include_router(runs.router)

__all__ = ["api_router"]
