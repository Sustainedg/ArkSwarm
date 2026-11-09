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
