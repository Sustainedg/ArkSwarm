"""测试夹具：每个用例都从干净的数据库开始。"""
from __future__ import annotations

import httpx
import pytest_asyncio

from app.db import engine, init_db
from app.main import app
from app.models import SQLModel


@pytest_asyncio.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def _drain_title_tasks():
    """每个用例收尾时取消还在跑的「自动起标题」后台任务。

    发一条消息就会丢一个后台任务出去（见 `runtime/titles.py`）：它要起一次真实内核。
    任务跨用例存活会占着数据库与子进程，把下一个用例的建库 / 删表堵成 database is locked。
    """
    yield
    from app.runtime import titles

    await titles.cancel_pending()
