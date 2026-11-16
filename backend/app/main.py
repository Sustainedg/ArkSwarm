"""FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, collab
from .agents import all_agents
from .api import api_router
from .bootstrap import ensure_default_agent, seed_if_empty
from .db import SessionLocal, init_db
from .runtime import availability as availability_service
from .runtime import skills as skill_service
from .settings import get_settings
from .transport.gzip import SSEExemptGZipMiddleware

logger = logging.getLogger("arkswarm")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


async def _scan_skills(session) -> None:
    """启动时把各内核的技能同步进统一目录；失败不影响服务启动。"""
    try:
        result = await skill_service.rescan(session)
        summary = (
            f"技能编目完成：新增 {result['added']}、更新 {result['updated']}、"
            f"目录已消失 {result['missing']}、共 {result['total']}"
        )
        logger.info("%s", summary)
    except Exception:  # noqa: BLE001 - 界面里还有「重新扫描」按钮可以重试
        logger.warning("技能编目失败，可在「技能」页点击重新扫描重试", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    async with SessionLocal() as session:
        await seed_if_empty(session, all_agents())
        # 老库升级上来时可能一颗星都没有：补一次，保证「新建对话」直接可用
        await ensure_default_agent(session)
        await _scan_skills(session)
        recovered = await collab.recover_interrupted(session)
        if recovered:
            logger.info("协作：%s 次运行因重启被标记为 interrupted", recovered)
    logger.info("%s %s 已就绪 — 数据目录 %s", settings.app_name, __version__, settings.data_dir)
    # 运行时可用性（内核装没装、什么版本）是缓存 + 定时检测的，绝不放在请求路径上：
    # 探测要起一次 `内核 --version` 子进程，同步跑会冻住整个事件循环。
    detector = asyncio.create_task(availability_service.scheduler())
    try:
        yield
    finally:
        detector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await detector


app = FastAPI(
    title="ArkSwarm 多智能体控制台",
    version=__version__,
    description="多智能体对话控制台：把 Claude Code、Codex、Gemini CLI 等 "
                "内核配成智能体，给它们挑选网关、模型与人格后进行对话。",
    lifespan=lifespan,
)

_settings = get_settings()
# gzip 放在 CORS 内侧：先压缩再补 CORS 头，浏览器才能正确解压跨域响应。
# 压缩只对普通响应生效：大 JSON（协作详情等）从几十 MB 裸传降到约 1/3。
# SSE（text/event-stream）由 SSEExemptGZipMiddleware 显式豁免：starlette 的
# GZipResponder 用 zlib 的 Z_NO_FLUSH 写流，中间分块取不到数据，事件、心跳与
# 打字机 delta 会被攒到响应收尾才下发；豁免后消息原样逐块透传，实时到达。
app.add_middleware(SSEExemptGZipMiddleware, minimum_size=1024, compresslevel=6)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.exception_handler(Exception)
async def unhandled(request, exc):  # pragma: no cover - 兜底保护
    logger.exception("未捕获的异常：%s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def resolve_spa_target(dist: Path, full_path: str) -> Path:
    """把前端深链解析到真实存在的 HTML 入口。

    桌面端（`/`）与移动端（`/m/`）是两条物理独立的链接，各有自己的构建入口：
    静态文件（构建产物、manifest、Service Worker）优先原样返回，其余路径回落到
    对应入口的 index.html，交给前端路由接管。
    """
    if full_path:
        candidate = dist / full_path
        if candidate.is_file():
            return candidate
    if full_path == "m" or full_path.startswith("m/"):
        mobile_entry = dist / "m" / "index.html"
        if mobile_entry.is_file():
            return mobile_entry
    return dist / "index.html"


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        """前端深链交给入口 HTML；但 `/api/` 下的漏网请求必须显式报 404。

        否则「不存在的接口」会拿到 200 + index.html（HTML）：前端 `response.json()`
        解析失败，WebKit 抛出的还是一句和业务毫无关系的
        "The string did not match the expected pattern."，极难排查。
        明确回一个 JSON 404，客户端就能直接告诉你「接口不存在」。
        """
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"接口不存在：/{full_path}")
        # 外壳（入口 HTML / sw.js / manifest）每次都回源校验：PWA 装到主屏后不会手动刷新，
        # 让浏览器或中间代理直接命中旧外壳，新构建就永远「看起来没生效」。
        # 带哈希的构建产物走 /assets 的静态挂载，不在这里，可以继续长缓存。
        return FileResponse(
            resolve_spa_target(FRONTEND_DIST, full_path),
            headers={"Cache-Control": "no-cache"},
        )
