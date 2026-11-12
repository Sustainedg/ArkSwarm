from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import VERSION_HISTORY, __version__
from ..agents import all_agents
from ..runtime import availability as availability_service
from ..runtime import registry
from ..settings import get_settings
from .architecture_doc import build_architecture_page

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/system/overview")
async def overview() -> dict:
    settings = get_settings()
    agents = all_agents()
    # 可用性读缓存（缺行只查 PATH）：这里绝不能实时探测，否则每次打开页面都冻住几秒。
    statuses = await availability_service.statuses(agents)
    return {
        "version": __version__,
        "version_history": [dict(item) for item in VERSION_HISTORY],
        "app_name": settings.app_name,
        "data_dir": str(settings.data_dir),
        "workspaces_root": str(settings.workspaces_root),
        "agent_types": len(agents),
        "installed_types": sum(1 for a in agents if statuses[a.id].installed),
        "active_runs": registry.list(),
    }


@router.get("/system/runs")
async def active_runs() -> dict:
    return {"runs": registry.list()}


@router.get("/system/architecture")
async def architecture() -> dict:
    """把 ``docs/architecture.md`` 实时渲染成 HTML，供界面「系统架构」按钮展示。

    每次请求都重新读取文档，因此本地更新文档后无需重启或重新构建即可同步。
    """
    try:
        return build_architecture_page()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"未找到架构文档：{exc.filename}") from None
