"""FastAPI 应用入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agents import all_agents
from .api import api_router
from .bootstrap import seed_if_empty
from .db import SessionLocal, init_db
from .settings import get_settings

logger = logging.getLogger("magenta")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    async with SessionLocal() as session:
        await seed_if_empty(session, all_agents())
    logger.info("%s %s 已就绪 — 数据目录 %s", settings.app_name, __version__, settings.data_dir)
    yield


app = FastAPI(
    title="Magenta Agents 多智能体控制台",
    version=__version__,
    description="多智能体对话控制台：把 Claude Code、Codex、Gemini CLI、DeepSeek 等 "
                "智能体配置成实例，再绑定到角色上进行对话。",
    lifespan=lifespan,
)

_settings = get_settings()
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


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
