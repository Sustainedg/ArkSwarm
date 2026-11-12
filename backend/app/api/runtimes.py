"""智能体运行时（各 CLI 内核）的安装 / 更新 / 卸载接口。

运行时 = 某个 CLI 适配器依赖的本机可执行文件。这里只做三件事：列出本机有哪些运行时、
汇报安装状态与当前版本、代用户执行安装 / 更新 / 卸载。具体命令由适配器声明
（``AgentDefinition.provisioner``），本层不认识任何具体内核（铁律 1）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..agents import AgentDefinition, all_agents, get_agent
from ..agents.base import Availability
from ..db import get_session
from ..models import AgentProfile
from ..runtime import availability as availability_service
from ..runtime import provisioning
from ..schemas import RuntimeAction
from ..transport.sse import sse_stream

router = APIRouter(prefix="/runtimes", tags=["runtimes"])


def _describe(definition: AgentDefinition, agents: int, availability: Availability) -> dict:
    """一张运行时卡片的全部数据；安装状态来自框架缓存的检测结果（不实时探测）。"""
    return {
        "id": definition.id,
        "name": definition.name,
        "vendor": definition.vendor,
        "description": definition.description,
        "runtime": definition.runtime.value,
        "docs_url": definition.docs_url,
        "logo": definition.logo,
        "executable": definition.executable,
        #: 有多少个未删除的智能体在用这个内核（卸载前提示影响面）
        "agents": agents,
        "provisioning": definition.provisioning(availability),
    }


@router.get("")
async def list_runtimes(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        await session.execute(
            select(AgentProfile.type_id, func.count(AgentProfile.id))
            .where(AgentProfile.deleted_at.is_(None))
            .group_by(AgentProfile.type_id)
        )
    ).all()
    counts = {type_id: count for type_id, count in rows}
    definitions = all_agents()
    statuses = await availability_service.statuses(definitions)
    return {
        "items": [
            _describe(definition, counts.get(definition.id, 0), statuses[definition.id])
            for definition in definitions
        ]
    }


@router.post("/{type_id}/refresh")
async def refresh_runtime(type_id: str) -> dict:
    """强制重测一次安装状态与版本（卡片上的「重新检测」按钮）。

    这是唯一会同步等待探测的读接口 —— 但探测在后台线程里跑，且这是用户主动触发的，
    因此不会像「每次打开页面都探测」那样拖慢整站。
    """
    try:
        return (await availability_service.refresh_type(type_id)).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _provision_stream(type_id: str, action: provisioning.Action) -> StreamingResponse:
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    label = provisioning.ACTION_LABELS[action]
    if definition.provisioner is None:
        raise HTTPException(status_code=409, detail=f"{definition.name} 不由平台管理，无法{label}")
    if not definition.provisioner.supports(action):
        raise HTTPException(status_code=409, detail=f"{definition.name} 未声明「{label}」命令")
    return StreamingResponse(
        sse_stream(provisioning.stream_provision(definition, action)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/{type_id}/install")
async def install_runtime(type_id: str) -> StreamingResponse:
    return _provision_stream(type_id, "install")


@router.post("/{type_id}/update")
async def update_runtime(type_id: str) -> StreamingResponse:
    return _provision_stream(type_id, "update")


@router.post("/{type_id}/uninstall")
async def uninstall_runtime(type_id: str, payload: RuntimeAction) -> StreamingResponse:
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="卸载属于破坏性操作，需要显式确认（confirm=true）")
    return _provision_stream(type_id, "uninstall")


@router.post("/{type_id}/check")
async def check_runtime(type_id: str) -> dict:
    """查询该运行时的最新可用版本（给界面「检查更新」按钮用）。"""
    try:
        definition = get_agent(type_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return await provisioning.latest_version(definition)
