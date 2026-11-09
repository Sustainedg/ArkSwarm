"""Async SQLite engine + session helpers."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from .settings import get_settings

_settings = get_settings()
engine = create_async_engine(
    _settings.sqlalchemy_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if _settings.sqlalchemy_url.startswith("sqlite") else {},
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """SQLite needs this to enforce the ON DELETE CASCADE rules."""
    if _settings.sqlalchemy_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


async def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
