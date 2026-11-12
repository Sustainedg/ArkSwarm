"""运行时可用性的缓存与定时检测（``runtime/availability.py``）。

这里的核心断言是「请求路径绝不实时探测」：探测要起一次 ``内核 --version`` 子进程，
gemini 这类 Node CLI 冷启动接近 2 秒，同步跑会冻住整个事件循环 —— 那正是
「打开网站要等很久才读到对话历史」的根因，必须有测试盯着。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
import pytest
import pytest_asyncio
from sqlmodel import select

from app.agents import AgentContext, AgentDefinition, AgentRun, Runtime, all_agents
from app.agents.base import Availability, RuntimeProvisioner
from app.db import SessionLocal, engine, init_db
from app.main import app
from app.models import RuntimeStatus, SQLModel
from app.runtime import availability as availability_service
from app.runtime import provisioning

#: 记录 ``availability()`` 被调用的次数（每次调用都是一次真实子进程探测）。
probes: list[str] = []


class _StubRuntime(AgentDefinition):
    """探测次数可观测的替身运行时（``executable = sh`` 保证 PATH 兜底能命中）。"""

    id = "stub-runtime"
    name = "替身运行时"
    description = "仅用于测试的运行时。"
    runtime = Runtime.CLI
    executable = "sh"

    def build_invocation(self, ctx: AgentContext):  # pragma: no cover - 契约占位
        raise NotImplementedError

    def parse_line(self, line: str, run: AgentRun) -> Iterable[Any]:  # pragma: no cover
        return ()

    def availability(self) -> Availability:
        probes.append(self.id)
        return Availability(True, version="1.2.3", detail="/bin/sh")


class _ExplodingRuntime(_StubRuntime):
    """探测直接抛错的替身：用来验证「一次失败不会把缓存里的旧结论改坏」。"""

    def availability(self) -> Availability:
        probes.append(self.id)
        raise RuntimeError("探测炸了")


def _stub_get_agent(definition: AgentDefinition):
    def get(type_id: str) -> AgentDefinition:
        if type_id != definition.id:
            raise KeyError(f"未知的运行时：{type_id}")
        return definition

    return get


@pytest_asyncio.fixture
async def db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch, db) -> _StubRuntime:
    """把可用性服务里的适配器目录换成替身，避免测试真的去探测本机内核。"""
    definition = _StubRuntime()
    probes.clear()
    availability_service.take_pending()
    monkeypatch.setattr(availability_service, "all_agents", lambda: [definition])
    monkeypatch.setattr(availability_service, "get_agent", _stub_get_agent(definition))
    return definition


@pytest_asyncio.fixture
async def client() -> httpx.AsyncClient:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _row(type_id: str) -> RuntimeStatus | None:
    async with SessionLocal() as session:
        return await session.get(RuntimeStatus, type_id)


# --- 缓存读写 ---------------------------------------------------------------


async def test_statuses_falls_back_without_probing_and_enqueues(stub: _StubRuntime):
    """没有缓存时用 PATH 兜底（微秒级），并把类型排进待补测队列。"""
    statuses = await availability_service.statuses([stub])
    assert probes == [], "读缓存阶段不得探测"
    assert statuses[stub.id].installed is True  # sh 在 PATH 里
    assert statuses[stub.id].version is None  # 版本要等真实探测
    assert statuses[stub.id].checked_at is None
    assert availability_service.take_pending() == [stub.id]


async def test_statuses_reads_cache_after_refresh(stub: _StubRuntime):
    result = await availability_service.refresh()
    assert result == {"checked": 1, "skipped": 0, "failed": 0}
    assert probes == [stub.id]

    probes.clear()
    statuses = await availability_service.statuses([stub])
    assert statuses[stub.id].version == "1.2.3"
    assert statuses[stub.id].checked_at is not None
    assert availability_service.take_pending() == [], "命中缓存不该再排队"
    assert probes == [], "命中缓存不得探测"


async def test_refresh_only_stale_skips_fresh_rows(stub: _StubRuntime):
    await availability_service.refresh()
    probes.clear()
    assert (await availability_service.refresh(only_stale=True))["skipped"] == 1
    assert probes == [], "结果还新鲜时不该重复探测"


async def test_expired_result_is_enqueued_again(stub: _StubRuntime):
    await availability_service.refresh()
    async with SessionLocal() as session:
        row = await session.get(RuntimeStatus, stub.id)
        assert row is not None
        row.checked_at = datetime.now(timezone.utc) - availability_service.CHECK_INTERVAL - timedelta(minutes=1)
        session.add(row)
        await session.commit()

    await availability_service.statuses([stub])
    assert availability_service.take_pending() == [stub.id], "过期结果要重新补测"
    assert probes == [stub.id], "读缓存阶段本身不探测"


async def test_failed_probe_keeps_previous_result(monkeypatch: pytest.MonkeyPatch, stub: _StubRuntime):
    await availability_service.refresh()
    monkeypatch.setattr(availability_service, "all_agents", lambda: [_ExplodingRuntime()])

    result = await availability_service.refresh()
    assert result["failed"] == 1 and result["checked"] == 0

    row = await _row(stub.id)
    assert row is not None
    assert row.installed is True and row.version == "1.2.3", "探测出错不得改坏上一次的结论"
    assert row.detail == "/bin/sh"


async def test_missing_probe_failure_leaves_no_row(monkeypatch: pytest.MonkeyPatch, stub: _StubRuntime):
    """首次探测就失败时不写行，下一次读表会重新排队（而不是永远显示未安装）。"""
    monkeypatch.setattr(availability_service, "all_agents", lambda: [_ExplodingRuntime()])
    assert (await availability_service.refresh())["failed"] == 1
    assert await _row(stub.id) is None


# --- 定时检测 ---------------------------------------------------------------


def test_seconds_until_daily_points_at_seven_local():
    tz = availability_service.LOCAL_TZ
    assert availability_service.seconds_until_daily(datetime(2026, 11, 12, 6, 0, tzinfo=tz)) == pytest.approx(3600)
    assert availability_service.seconds_until_daily(datetime(2026, 11, 12, 9, 0, tzinfo=tz)) == pytest.approx(22 * 3600)
    # 正好 7 点整 → 排到第二天，避免同一分钟内反复触发。
    assert availability_service.seconds_until_daily(datetime(2026, 11, 12, 7, 0, tzinfo=tz)) == pytest.approx(24 * 3600)


# --- 接口与安装器 -----------------------------------------------------------


async def test_request_path_never_probes(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """所有读接口只读缓存：让 availability() 直接报错，接口也必须正常响应。"""

    def fail(*_args: Any, **_kwargs: Any) -> Availability:
        raise AssertionError("请求路径不得实时探测可用性（会冻住整个事件循环）")

    for definition in all_agents():
        monkeypatch.setattr(type(definition), "availability", fail)

    for path in ("/api/system/overview", "/api/agent-types", "/api/agents", "/api/runtimes"):
        response = await client.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"


async def test_refresh_endpoint_probes_on_demand(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    definition = _StubRuntime()
    probes.clear()
    monkeypatch.setattr(availability_service, "all_agents", lambda: [definition])
    monkeypatch.setattr(availability_service, "get_agent", _stub_get_agent(definition))

    payload = (await client.post("/api/runtimes/stub-runtime/refresh")).json()
    assert payload["installed"] is True and payload["version"] == "1.2.3"
    assert probes == [definition.id], "「重新检测」必须真实探测一次"
    assert payload["detail"] == "/bin/sh"
    assert str(payload["checked_at"]).endswith("+00:00"), "检测时间必须带时区，否则界面会整体偏移"


async def test_refresh_endpoint_rejects_unknown_runtime(client: httpx.AsyncClient):
    assert (await client.post("/api/runtimes/nope/refresh")).status_code == 404


async def test_refresh_definition_tolerates_unregistered_definition(
    monkeypatch: pytest.MonkeyPatch, stub: _StubRuntime
):
    """适配器不在注册表里（测试替身、动态定义）时只探测、不写缓存，但必须给得出结果。"""

    def missing(type_id: str) -> AgentDefinition:
        raise KeyError(f"未知的运行时：{type_id}")

    monkeypatch.setattr(availability_service, "get_agent", missing)
    probes.clear()
    status = await availability_service.refresh_definition(stub)
    assert status.installed is True and status.version == "1.2.3"
    assert probes == [stub.id]
    assert await _row(stub.id) is None


async def test_install_refreshes_the_cache(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """安装 / 更新 / 卸载都会改本机环境，收尾前必须顺手重测缓存。"""
    definition = _StubRuntime()
    definition.provisioner = RuntimeProvisioner(manager="test", install_argv=["sh", "-c", "echo 安装完成"])
    probes.clear()
    monkeypatch.setattr(availability_service, "all_agents", lambda: [definition])
    monkeypatch.setattr(availability_service, "get_agent", _stub_get_agent(definition))

    events = [event async for event in provisioning.stream_provision(definition, "install")]
    final = events[-1]
    assert final["type"] == "provision.finished" and final["ok"] is True
    assert final["installed"] is True and final["version"] == "1.2.3"

    row = await _row(definition.id)
    assert row is not None and row.checked_at is not None and row.version == "1.2.3"
