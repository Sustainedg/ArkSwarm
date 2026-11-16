"""技能：统一编目与智能体分配。

技能目录由各智能体适配器只读扫描，平台把它们汇成一份统一目录；智能体能用的技能
只能通过这里的分配关系获得（``agent_skills`` 表）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import get_agent
from ..db import get_session
from ..models import Skill, User
from ..runtime import skills as skill_service
from ..schemas import SkillDetail, SkillRead, SkillRescanResult
from .auth import current_user, require_admin

router = APIRouter(prefix="/skills", tags=["skills"])


def _type_name(type_id: str) -> str | None:
    try:
        return get_agent(type_id).name
    except KeyError:
        return None


def _describe(row: Skill, counts: dict[str, int]) -> SkillRead:
    return SkillRead(
        id=row.id,
        type_id=row.type_id,
        type_name=_type_name(row.type_id),
        slug=row.slug,
        name=row.name,
        description=row.description or "",
        source_kind=row.source_kind,
        source_label=skill_service.SOURCE_LABELS.get(row.source_kind, row.source_kind),
        version=row.version,
        author=row.author,
        when_to_use=row.when_to_use,
        available=row.available,
        total_bytes=row.total_bytes or 0,
        file_count=len(row.files or []),
        note=row.note,
        used_by_agents=counts.get(row.id, 0),
        is_market=row.source_kind == skill_service.MARKET_SOURCE,
        deletable=skill_service.is_deletable(row),
        source_url=(row.source_url or None),
    )


@router.get("")
async def list_skills(
    type_id: str | None = None,
    source_kind: str | None = None,
    available: bool | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """列出所有内核的技能（可按智能体类型 / 来源 / 可用性 / 关键词过滤）。

    技能是平台级共享数据（从各内核扫描得出），所有登录用户都能看到。
    """
    rows = await skill_service.list_rows(
        session, type_id=type_id, source_kind=source_kind, available=available, query=q
    )
    counts = await skill_service.usage_counts(session)
    return {
        "items": [_describe(row, counts).model_dump() for row in rows],
        "source_labels": skill_service.SOURCE_LABELS,
    }


@router.post("/rescan")
async def rescan_skills(
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """重新扫描各内核的技能目录，并同步到统一目录。"""
    result = await skill_service.rescan(session)
    return SkillRescanResult(**result).model_dump()


@router.get("/{skill_id}")
async def get_skill(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    row = await skill_service.get_row(session, skill_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"技能不存在：{skill_id}")
    counts = await skill_service.usage_counts(session)
    body, truncated = skill_service.read_body(row.path)
    detail = SkillDetail(
        **_describe(row, counts).model_dump(),
        path=row.path,
        fingerprint=row.fingerprint or "",
        allowed_tools=list(row.allowed_tools or []),
        files=list(row.files or []),
        meta=dict(row.meta or {}),
        body=body,
        body_truncated=truncated,
        agents=[
            {"id": profile.id, "name": profile.name, "avatar": profile.avatar}
            for profile in await skill_service.agents_using(session, skill_id)
        ],
    )
    return detail.model_dump()


@router.delete("/{skill_id}", dependencies=[Depends(require_admin)])
async def delete_skill(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
    user: Annotated[User, Depends(current_user)] = None,
) -> dict:
    """删除技能：内核自带（system）之外的技能都允许删，同时断开智能体分配。"""
    try:
        name = await skill_service.delete_skill(session, skill_id)
    except skill_service.SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, "name": name}
