"""自更新 API：检测新版本、一键更新、回滚。

界面入口在「设置 → 关于」：检测更新（查 GitHub 最新 Release）→ 更新（下载、解压、
切指针、原地重启）。本层保持薄：动作全在 :mod:`app.selfupdate`，这里只做状态码
翻与 SSE 封装。

安装 / 回滚是长动作（下载几十 MB 的包），用与运行时安装同构的 SSE 流式回显：
事件信封是本模块自有的 ``update.*``（不属于适配器事件协议），逐条给出阶段与
结果，最后一条之后进程原地重启。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import __version__, selfupdate
from ..selfupdate import SelfUpdateError
from .auth import require_admin

logger = logging.getLogger("arkswarm.selfupdate")

#: 整个路由器都要求登录：自更新是平台级操作。
router = APIRouter(prefix="/update", tags=["update"], dependencies=[Depends(require_admin)])


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    """产出一条 ``update.*`` 事件（本模块自有信封，只在关于页消费）。"""
    return {"type": f"update.{kind}", **payload}


def _line(payload: dict[str, Any]) -> bytes:
    """一条 SSE data 行。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


@router.get("/check")
async def check() -> dict:
    """检测 GitHub 最新 Release；返回更新面板要的完整状态。"""
    try:
        release = await selfupdate.check_latest()
    except SelfUpdateError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return selfupdate.status(release)


@router.post("/install")
async def install() -> StreamingResponse:
    """下载并安装最新 Release，然后原地重启（SSE 流式回显进度）。"""
    async def stream() -> AsyncIterator[bytes]:
        yield _line(_event("started", current=__version__))
        try:
            # 源码模式先拦：不用查 GitHub 就知道装不了，提示也更快更准
            if selfupdate.managed_root() is None:
                yield _line(
                    _event(
                        "error",
                        message="当前是源码模式（没有 releases/current 结构），请用 git 更新；"
                        "托管模式（Docker / 安装包）才支持一键更新",
                    )
                )
                return
            release = await selfupdate.check_latest()
            if release is None:
                yield _line(_event("error", message="最新 Release 不是版本发布（缺 tag 或产物包），无法安装"))
                return
            if selfupdate.version_eq(release.version, __version__):
                yield _line(_event("error", message=f"{release.version} 就是当前版本，无需更新"))
                return
            if not selfupdate.version_gt(release.version, __version__):
                yield _line(_event("error", message=f"最新版本 {release.version} 不高于当前 {__version__}，如需换装请回滚"))
                return
            yield _line(
                _event(
                    "downloading",
                    version=release.version,
                    size=release.asset_size,
                    published_at=release.published_at,
                    notes=release.notes,
                )
            )
            result = await selfupdate.install_release(release)
            yield _line(_event("restarting", **result))
            # 响应发出后再安排 exec：SSE 生成器走到这里说明事件已产出，
            # 给一点时间让最后一帧落地，再原地重启进程
            await asyncio.sleep(0.5)
            selfupdate.schedule_restart(delay_seconds=0.3)
        except SelfUpdateError as exc:
            yield _line(_event("error", message=str(exc)))
        except Exception:  # noqa: BLE001 - 兜底也要让界面看到失败原因
            logger.exception("自更新安装失败")
            yield _line(_event("error", message="安装失败，详情见服务日志"))

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/rollback")
async def rollback() -> StreamingResponse:
    """回滚到上一版本并原地重启（SSE 同 install）。"""
    async def stream() -> AsyncIterator[bytes]:
        try:
            result = await selfupdate.rollback()
            yield _line(_event("restarting", **result))
            await asyncio.sleep(0.5)
            selfupdate.schedule_restart(delay_seconds=0.3)
        except SelfUpdateError as exc:
            yield _line(_event("error", message=str(exc)))
        except Exception:  # noqa: BLE001
            logger.exception("自更新回滚失败")
            yield _line(_event("error", message="回滚失败，详情见服务日志"))

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/status")
async def update_status() -> dict:
    """本地状态（不查 GitHub）：模式、已装版本、可否回滚、上次重启记录。"""
    try:
        return selfupdate.status()
    except Exception as exc:  # noqa: BLE001 - 状态接口不该 500
        raise HTTPException(status_code=500, detail=f"读取更新状态失败：{exc}") from None
