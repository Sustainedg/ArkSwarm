"""数据库结构：后加的可选列必须能补到**已存在**的旧库上，且重复启动幂等。

SQLite 的 ``create_all`` 只建新表、不会给旧表加列，所以"后加的可选列"登记在
``models.ADDITIVE_COLUMNS``（对话域）与 ``collab.tables.ADDITIVE_COLUMNS``（协作域），
由 ``db.init_db`` 幂等补齐（见 AGENTS.md 铁律 9）。
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

import app.db as db

OLD_SCHEMA = """
CREATE TABLE agent_profiles (
    id VARCHAR NOT NULL PRIMARY KEY,
    name VARCHAR(120) NOT NULL
)
"""


@pytest_asyncio.fixture
async def old_db(monkeypatch, tmp_path):
    """一个只含"旧版 agent_profiles"表的临时库，并把 ``db.engine`` 指向它。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(OLD_SCHEMA)
        await conn.exec_driver_sql(
            "INSERT INTO agent_profiles (id, name) VALUES ('老智能体', '历史智能体')"
        )
    monkeypatch.setattr(db, "engine", engine)
    yield engine
    await engine.dispose()


async def test_additive_columns_are_applied_to_an_existing_table(old_db):
    async with old_db.begin() as conn:
        await conn.run_sync(db._add_missing_columns, {"agent_profiles": {"deleted_at": "DATETIME"}})

        columns = {
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(agent_profiles)")).all()
        }
        row = (
            await conn.exec_driver_sql(
                "SELECT name, deleted_at FROM agent_profiles WHERE id = '老智能体'"
            )
        ).one()

    assert "deleted_at" in columns, "旧库缺列时应当自动补上"
    assert tuple(row) == ("历史智能体", None), "补列不能改动旧行"


async def test_additive_columns_are_idempotent(old_db):
    """重复启动（补列跑多次）不能报错、也不能重复加列。"""
    mapping = {"agent_profiles": {"deleted_at": "DATETIME"}}
    async with old_db.begin() as conn:
        await conn.run_sync(db._add_missing_columns, mapping)
        await conn.run_sync(db._add_missing_columns, mapping)
        columns = [
            row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(agent_profiles)")).all()
        ]
    assert columns.count("deleted_at") == 1


async def test_init_db_creates_the_domain_tables(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    monkeypatch.setattr(db, "engine", engine)
    try:
        await db.init_db()
        async with engine.begin() as conn:
            tables = {
                row[0]
                for row in (
                    await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
                ).all()
            }
    finally:
        await engine.dispose()
    assert {
        "agent_profiles",
        "gateways",
        "agent_skills",
        "conversations",
        "skills",
        "credentials",
        "agent_credentials",
    } <= tables
