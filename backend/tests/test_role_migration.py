"""数据库向后兼容：对话域后来新增的可选列（角色软删除标记）必须能补到**已存在**的旧库上。

背景与 ``test_collab_migration.py`` 相同：SQLite 的 ``create_all`` 只建新表、不会给旧表加列，
而本地库 ``backend/data/magenta.db`` 含真实数据、禁止重建，所以"后加的可选列"登记在
``models.ADDITIVE_COLUMNS``，由 ``db.init_db`` 幂等补齐（铁律 9）。
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

import app.db as db

#: 模拟"更早版本"建出来的 roles（没有 deleted_at 列）
OLD_SCHEMA = """
CREATE TABLE roles (
    id VARCHAR NOT NULL PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    avatar VARCHAR(16) NOT NULL DEFAULT '🛠️',
    tagline VARCHAR(200),
    system_prompt TEXT NOT NULL DEFAULT '',
    agent_id VARCHAR(64) NOT NULL,
    workspace_dir VARCHAR(500),
    parameters JSON NOT NULL DEFAULT '{}',
    created_at DATETIME,
    updated_at DATETIME
)
"""


@pytest_asyncio.fixture
async def old_db(monkeypatch, tmp_path):
    """一个只含"旧版 roles"表的临时库，并把 ``db.init_db`` 指向它。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(OLD_SCHEMA)
        await conn.exec_driver_sql(
            "INSERT INTO roles (id, name, agent_id) VALUES ('旧角色', '历史角色', 'agent-1')"
        )
    monkeypatch.setattr(db, "engine", engine)
    yield engine
    await engine.dispose()


async def test_deleted_at_is_added_to_an_existing_roles_table(old_db):
    await db.init_db()

    async with old_db.begin() as conn:
        columns = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(roles)")).all()}
        row = (
            await conn.exec_driver_sql("SELECT name, deleted_at FROM roles WHERE id = '旧角色'")
        ).one()

    assert "deleted_at" in columns, "旧库缺 deleted_at 列时 init_db 应当自动补上"
    assert tuple(row) == ("历史角色", None), "补列不能改动旧行"


async def test_migration_is_idempotent(old_db):
    """重复启动（init_db 跑多次）不能报错、也不能重复加列。"""
    await db.init_db()
    await db.init_db()

    async with old_db.begin() as conn:
        columns = [row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(roles)")).all()]
    assert columns.count("deleted_at") == 1
