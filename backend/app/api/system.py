from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from ..agents import all_agents
from ..runtime import registry
from ..settings import get_settings

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/system/overview")
async def overview() -> dict:
    settings = get_settings()
    agents = all_agents()
    return {
        "version": __version__,
        "app_name": settings.app_name,
        "data_dir": str(settings.data_dir),
        "workspaces_root": str(settings.workspaces_root),
        "agent_types": len(agents),
        "installed_types": sum(1 for a in agents if a.availability().installed),
        "active_runs": registry.list(),
    }


@router.get("/system/runs")
async def active_runs() -> dict:
    return {"runs": registry.list()}
