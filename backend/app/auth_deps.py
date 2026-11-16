"""鉴权依赖项：解析请求里的 Bearer 令牌，返回当前用户。

刻意放在顶层 ``app/`` 而不是 ``app/api/`` 下：协作域（``app/collab/``）有架构守卫
禁止依赖 ``app.api.*``，但协作域的路由同样需要鉴权，所以这份依赖项必须住在协作域
允许依赖的层里（与 ``models`` / ``db`` / ``settings`` 同层）。

用户管理接口（登录 / 注册 / 用户 CRUD）仍在 ``app/api/auth.py``，那才是 ``api`` 层；
这里只提供「取当前用户」「要求管理员」「检查归属」三个依赖项与虚拟管理员。
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Query, Request
from sqlmodel import select

from .auth import TokenError, parse_token
from .db import SessionLocal
from .models import User
from .settings import get_settings

#: 虚拟管理员：鉴权关闭时所有接口视为管理员调用。id 用固定串，避免与真实用户 id 冲突。
_SYSTEM_USER = User(
    id="__system__",
    username="__system__",
    display_name="系统",
    role="admin",
    password_hash="",
    is_active=True,
)


async def current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Query()] = None,
) -> User:
    """解析请求里的 Bearer 令牌，返回当前用户。

    鉴权关闭时返回虚拟管理员；令牌缺失 / 过期 / 无效时抛 401。

    ``access_token`` 查询参数只为 ``<img src>`` 这类**无法自定义请求头**的资源请求保留
    （对话工作目录里的图片、附件预览）。普通接口一律走 Authorization 头，不要把令牌
    拼进 URL —— URL 会进访问日志与浏览器历史。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return _SYSTEM_USER
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer ") :]
    elif access_token:
        # 查询参数令牌只给「浏览器会自己发起的资源请求」用；普通接口一律走请求头。
        if "/files/" not in request.url.path:
            raise HTTPException(status_code=401, detail="令牌只能放在 Authorization 请求头里")
        token = access_token
    if not token:
        raise HTTPException(status_code=401, detail="未登录或令牌缺失")
    try:
        payload = parse_token(token)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="令牌载荷不完整")
    async with SessionLocal() as session:
        user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不存在或已被禁用")
    # 改口令 / 重置口令会 +1：旧令牌即使未过期也不再有效。
    if payload.get("ver", 0) != (user.token_version or 0):
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    return user


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    """要求当前用户是管理员；否则 403。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def owner_id_for(user: User) -> str | None:
    """写入资产时该记的 ``owner_id``：鉴权关闭时记空（老库语义），不要写虚拟管理员 id。

    这样之后把 ``ARKSWARM_AUTH_ENABLED`` 打开，存量行仍能被 ``backfill_ownership``
    回填给首任管理员，而不是留在一个永远登录不了的 ``__system__`` 名下。
    """
    return None if user.id == _SYSTEM_USER.id else user.id


def require_owned(row: Any, user: User) -> None:
    """检查某行资产是否属于当前用户；不属于则 404（不说 403，避免泄露「存在但无权」）。

    管理员跳过检查。鉴权关闭时 ``user`` 是虚拟管理员，同样跳过。
    """
    if user.role == "admin":
        return
    if getattr(row, "owner_id", None) != user.id:
        raise HTTPException(status_code=404, detail="资源不存在或无权访问")


__all__ = ["current_user", "require_admin", "require_owned", "owner_id_for", "_SYSTEM_USER"]