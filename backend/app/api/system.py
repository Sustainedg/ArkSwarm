from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pydantic import BaseModel
from sqlmodel import select

from datetime import datetime, timezone

from .. import VERSION_HISTORY, __version__
from ..agents import all_agents
from ..db import SessionLocal
from ..models import PlatformSetting
from ..runtime import availability as availability_service
from ..runtime import registry
from ..settings import get_settings
from .architecture_doc import build_architecture_page

router = APIRouter(tags=["system"])


class ProxySettings(BaseModel):
    """网络代理设置（注入到安装 / 更新等子进程的环境变量里）。"""

    proxy_url: str | None = None


async def get_proxy_url() -> str | None:
    """从数据库读取代理地址；不存在或为空则返回 None。"""
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(PlatformSetting).where(PlatformSetting.key == "proxy_url")
            )
        ).scalar_one_or_none()
        return row.value if row and row.value else None


@router.get("/system/proxy")
async def get_proxy() -> dict:
    """获取当前代理设置。"""
    url = await get_proxy_url()
    return {"proxy_url": url}


@router.put("/system/proxy")
async def set_proxy(payload: ProxySettings) -> dict:
    """保存代理设置。空字符串视为清除。"""
    value = (payload.proxy_url or "").strip() or None
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(PlatformSetting).where(PlatformSetting.key == "proxy_url")
            )
        ).scalar_one_or_none()
        if row is None:
            row = PlatformSetting(key="proxy_url", value=value)
            session.add(row)
        else:
            row.value = value
            row.updated_at = datetime.now(timezone.utc)
        await session.commit()
    return {"proxy_url": value}


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
