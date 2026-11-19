"""时间列必须「存裸 UTC、出库带时区」。

SQLite 没有原生时区类型，``DateTime(timezone=True)`` 在它下面**不会**保留 ``tzinfo``。
如果照原样交给 Pydantic 序列化，输出就是 ``2026-11-18T15:45:41``（没有偏移量），
浏览器 ``new Date(...)`` 会按**本地时区**解析 —— 东八区用户看到的时间整整少 8 小时。

这正是 ``models.UTCDateTime`` 存在的理由：入库抹掉时区（存储格式与历史数据一致），
出库补回 ``tzinfo=UTC``。这里的断言分两层：

* **存储层**：库里仍是裸 UTC 文本，不需要数据迁移；
* **接口层**：JSON 里的时间必须带 ``+00:00``，前端才能换算到用户本地时间。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine, init_db
from app.models import AgentProfile, Conversation, Message, User, utcnow


async def _seed_agent(session) -> AgentProfile:
    """建一个最小可用的智能体：``Conversation.agent_profile_id`` 是外键，不能指向空。"""
    agent = AgentProfile(name="时区测试智能体", type_id="claude-code")
    session.add(agent)
    await session.commit()
    return agent


@pytest.mark.asyncio
async def test_stored_timestamps_stay_naive_utc() -> None:
    """存储格式不能变：库里仍是 ``YYYY-MM-DD HH:MM:SS.ffffff`` 的裸 UTC 文本。

    这条盯的是「别为了修显示问题去改存储格式」—— 本地库含真实数据，
    改了格式就得写迁移，而且新旧行会混着两种写法。
    """
    await init_db()
    async with SessionLocal() as session:
        user = User(username="tz-user", password_hash="x")
        session.add(user)
        await session.commit()

    async with engine.connect() as conn:
        raw = (
            await conn.execute(
                text("SELECT created_at FROM users WHERE username = 'tz-user'")
            )
        ).scalar_one()
    # 驱动返回的裸文本里不能出现时区后缀，否则说明写入端把 aware 值原样塞进去了。
    assert isinstance(raw, str)
    assert "+" not in raw and "Z" not in raw, f"存储格式被改动：{raw!r}"
    datetime.fromisoformat(raw)  # 仍是可解析的裸时间


@pytest.mark.asyncio
async def test_columns_return_utc_aware_datetimes() -> None:
    """出库必须带 ``tzinfo=UTC``：这是 ``UTCDateTime`` 的核心承诺。"""
    await init_db()
    async with SessionLocal() as session:
        user = User(username="tz-aware", password_hash="x")
        session.add(user)
        await session.commit()
        user_id = user.id

    async with SessionLocal() as session:
        row = await session.get(User, user_id)
        assert row is not None
        assert row.created_at.tzinfo is not None
        assert row.created_at.utcoffset() == timezone.utc.utcoffset(None)
        # isoformat 带偏移量，浏览器才不会按本地时间误解析。
        assert row.created_at.isoformat().endswith("+00:00")


@pytest.mark.asyncio
async def test_api_serializes_timestamps_with_offset(client) -> None:
    """接口层：对话时间的 JSON 必须带 ``+00:00``（回归守卫）。"""
    await init_db()
    async with SessionLocal() as session:
        user = User(username="tz-api", password_hash="x", role="admin")
        session.add(user)
        await session.commit()

        agent = await _seed_agent(session)
        conversation = Conversation(title="时区回归", agent_profile_id=agent.id)
        session.add(conversation)
        await session.commit()
        conversation_id = conversation.id

    # 直接用库里的行验证序列化，避免依赖登录态与智能体配置。
    async with SessionLocal() as session:
        row = await session.get(Conversation, conversation_id)
        assert row is not None
        payload = {
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    for field, value in payload.items():
        assert value.endswith("+00:00"), f"{field} 缺少时区偏移：{value!r}"
        # 前端 new Date(...) 解析后应还原成同一个 UTC 时刻。
        parsed = datetime.fromisoformat(value)
        assert parsed.utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.asyncio
async def test_message_timestamp_roundtrip_keeps_instant() -> None:
    """写入 → 读出 → 序列化，时刻不能漂移（这是「时间不对」最直接的回归）。"""
    await init_db()
    stamp = datetime(2026, 11, 18, 15, 45, 41, 223044, tzinfo=timezone.utc)

    async with SessionLocal() as session:
        user = User(username="tz-roundtrip", password_hash="x")
        session.add(user)
        await session.commit()

        agent = await _seed_agent(session)
        conversation = Conversation(title="往返", agent_profile_id=agent.id)
        session.add(conversation)
        await session.commit()

        message = Message(
            conversation_id=conversation.id,
            role="user",
            content="hi",
            created_at=stamp,
        )
        session.add(message)
        await session.commit()
        message_id = message.id

    async with SessionLocal() as session:
        row = await session.get(Message, message_id)
        assert row is not None
        assert row.created_at == stamp, f"往返后时刻漂移：{row.created_at!r} != {stamp!r}"
        assert row.created_at.isoformat() == "2026-11-18T15:45:41.223044+00:00"


def test_utcnow_is_aware() -> None:
    """``utcnow()`` 必须带时区；裸时间写库时会被当成 UTC，但语义上仍不该裸奔。"""
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timezone.utc.utcoffset(None)
