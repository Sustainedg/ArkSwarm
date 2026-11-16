"""技能市场（SkillHub）：浏览、安装、卸载。

把 https://skillhub.cn 的技能接进「技能」页：浏览 / 搜索 / 安装到某个内核，并卸载
由平台装入的（``source_kind=market``）技能。安装落在内核的原生技能根里——**装完仍需
在智能体配置里分配**，运行时才会生效（与内核自带技能同一套规则）。

本层保持薄：只做参数校验、调用 :mod:`app.skillhub` 与 :mod:`app.runtime.skills`、
并把结果翻成 HTTP 状态码。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import all_agents
from ..db import get_session
from ..runtime import skills as skill_service
from ..schemas import SkillHubInstallIn, SkillHubUninstallIn
from ..skillhub import SkillHubError
from ..skillhub import download_skill as fetch_skill_archive
from ..skillhub import get_skill as fetch_skill_detail
from ..skillhub import list_skills as fetch_skill_list
from .auth import current_user, require_admin

#: 整个路由器都要求登录：技能市场是平台级操作。
router = APIRouter(prefix="/skillhub", tags=["skillhub"], dependencies=[Depends(current_user)])


@router.get("/skills")
async def market_skills(
    page: int = 1,
    page_size: int = 24,
    sort_by: str = "score",
    order: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    source: str | None = None,
) -> dict:
    """从技能市场拉一页技能（按评分 / 下载量 / 更新时间排序，可搜索）。"""
    try:
        return await fetch_skill_list(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            order=order,
            keyword=keyword,
            category=category,
            source=source,
        )
    except SkillHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.get("/kernels")
async def market_kernels() -> dict:
    """哪些内核可以安装技能（声明了可写技能根的适配器），供安装时的目标选择。"""
    items: list[dict[str, str | None]] = []
    for definition in all_agents():
        try:
            if not any(root.installable for root in definition.skill_roots()):
                continue
            root = skill_service.install_root_for_type(definition.id)
        except skill_service.SkillError:
            continue
        items.append({"type_id": definition.id, "name": definition.name, "path": root.path})
    return {"items": items}


@router.get("/skills/{slug}")
async def market_skill_detail(slug: str) -> dict:
    """技能市场里一个技能的详情。"""
    try:
        return await fetch_skill_detail(slug)
    except SkillHubError as exc:
        status = 404 if "不存在" in str(exc) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from None


@router.post("/install", dependencies=[Depends(require_admin)])
async def install_market_skill(
    payload: SkillHubInstallIn, session: AsyncSession = Depends(get_session)
) -> dict:
    """把一个市场技能下载并安装到指定内核的原生技能根。"""
    slug = (payload.slug or "").strip()
    if not slug:
        raise HTTPException(status_code=422, detail="缺少技能标识（slug）")

    version = payload.version
    requires_api_key = False
    try:
        detail = await fetch_skill_detail(slug)
        requires_api_key = bool(detail.get("requires_api_key"))
        if not version:
            version = detail.get("version") or None
    except SkillHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    try:
        archive = await fetch_skill_archive(slug, version=version)
    except SkillHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    try:
        result = await skill_service.install_market_skill(
            session,
            type_id=payload.type_id,
            slug=slug,
            version=version,
            archive=archive,
            source_url=f"https://skillhub.cn/skills/{slug}",
            display_slug=slug,
        )
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return {
        **result,
        "slug": slug,
        "type_id": payload.type_id,
        "version": version,
        "requires_api_key": requires_api_key,
    }


@router.post("/uninstall", dependencies=[Depends(require_admin)])
async def uninstall_market_skill(
    payload: SkillHubUninstallIn, session: AsyncSession = Depends(get_session)
) -> dict:
    """卸载一个由市场装入的技能（只删平台装进去的目录）。"""
    try:
        name = await skill_service.uninstall_market_skill(session, payload.skill_id)
    except skill_service.SkillNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except skill_service.SkillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"ok": True, "name": name}
