"""运行时（CLI 内核）管理：适配器契约、安装器执行与接口行为。

安装/卸载会改本机环境，所以测试**绝不**通过接口真的去跑 npm：执行层一律用
``sh -c echo`` 之类的无害命令验证；接口层面只覆盖 404 / 409 / 422 这些「不执行」的分支。
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any, Iterable

import httpx
import pytest
import pytest_asyncio

from app.agents import AgentContext, AgentDefinition, AgentRun, Runtime, all_agents, get_agent
from app.agents.base import RuntimeProvisioner
from app.db import engine, init_db
from app.main import app
from app.models import SQLModel
from app.runtime import provisioning


class _FakeRuntime(AgentDefinition):
    """仅用于测试的运行时定义：命令全是无害的 shell 回显。"""

    id = "fake-runtime"
    name = "假运行时"
    description = "测试用的运行时。"
    runtime = Runtime.CLI
    executable = "sh"
    provisioner = RuntimeProvisioner(
        manager="test",
        package="fake-runtime",
        install_argv=["sh", "-c", "echo 安装完成"],
        update_argv=["sh", "-c", "echo 更新完成"],
        uninstall_argv=["sh", "-c", "echo 卸载完成"],
        latest_argv=["sh", "-c", "echo 9.9.9"],
        homepage="https://example.com",
    )

    def build_invocation(self, ctx: AgentContext):  # pragma: no cover - 契约占位
        raise NotImplementedError

    def parse_line(self, line: str, run: AgentRun) -> Iterable[Any]:  # pragma: no cover
        return ()


class _NoCommandRuntime(_FakeRuntime):
    """声明了 provisioner，但每个动作都没有命令。"""

    id = "fake-empty"
    provisioner = RuntimeProvisioner(manager="test")


@pytest_asyncio.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _collect(definition: AgentDefinition, action: str) -> list[dict]:
    return [event async for event in provisioning.stream_provision(definition, action)]  # type: ignore[arg-type]


# --- 适配器契约 -------------------------------------------------------------


def test_cli_kernels_declare_a_provisioner():
    """三个 CLI 内核都要声明安装方式，否则「运行时」页无从下手。"""
    for type_id in ("claude-code", "codex", "gemini-cli"):
        provisioner = get_agent(type_id).provisioner
        assert provisioner is not None, f"{type_id} 未声明 provisioner"
        assert provisioner.manager and provisioner.package, f"{type_id} 缺少包管理器/包名"
        for action in ("install", "update", "uninstall"):
            assert provisioner.supports(action), f"{type_id} 缺少「{action}」命令"
        assert provisioner.latest_argv, f"{type_id} 缺少「查询最新版本」命令"


def test_api_kernels_are_not_managed():
    """进程内 API 内核与自定义 CLI 不该被平台改环境。"""
    for type_id in ("openai-harness", "custom-cli"):
        assert get_agent(type_id).provisioner is None, f"{type_id} 不应声明 provisioner"
        assert get_agent(type_id).provisioning()["supported"] is False


def test_provisioning_descriptor_is_serialisable():
    for definition in all_agents():
        data = definition.provisioning()
        assert set(data) == {
            "supported", "manager", "package", "homepage", "note", "commands",
            "installed", "version", "path",
        }, f"{definition.id} 的 provisioning() 字段集发生变化"


# --- 执行层 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_provision_runs_and_reports_success():
    events = await _collect(_FakeRuntime(), "install")
    assert events[0]["type"] == "provision.started"
    assert "sh" in events[0]["command"]
    output = "".join(e.get("text", "") for e in events if e["type"] == "provision.output")
    assert "安装完成" in output
    final = events[-1]
    assert final["type"] == "provision.finished"
    assert final["ok"] is True and final["status"] == "ok" and final["exit_code"] == 0


@pytest.mark.asyncio
async def test_stream_provision_reports_failure():
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(manager="test", install_argv=["sh", "-c", "exit 3"])
    events = await _collect(definition, "install")
    final = events[-1]
    assert final["type"] == "provision.finished"
    assert final["ok"] is False and final["status"] == "error" and final["exit_code"] == 3


@pytest.mark.asyncio
async def test_stream_provision_without_command_is_explicit():
    events = await _collect(_NoCommandRuntime(), "install")
    assert events[-1]["type"] == "provision.finished"
    assert events[-1]["ok"] is False
    assert "未声明" in events[-1]["error"]


@pytest.mark.asyncio
async def test_stream_provision_without_provisioner_is_explicit():
    events = await _collect(get_agent("custom-cli"), "install")
    assert events[-1]["ok"] is False
    assert "不由平台管理" in events[-1]["error"]


@pytest.mark.asyncio
async def test_latest_version_reads_last_line():
    result = await provisioning.latest_version(_FakeRuntime())
    assert result["supported"] is True
    assert result["latest"] == "9.9.9"
    assert result["error"] is None


@pytest.mark.asyncio
async def test_latest_version_reports_failure_as_data():
    definition = _FakeRuntime()
    definition.provisioner = RuntimeProvisioner(manager="test", latest_argv=["sh", "-c", "exit 7"])
    result = await provisioning.latest_version(definition)
    assert result["supported"] is True
    assert result["latest"] is None
    assert result["error"]


@pytest.mark.asyncio
async def test_latest_version_unsupported_without_command():
    result = await provisioning.latest_version(_NoCommandRuntime())
    assert result["supported"] is False and result["latest"] is None


# --- 取消与超时（铁律 10）----------------------------------------------------


def _sleeper() -> RuntimeProvisioner:
    """一条「打印 PID 后长睡」的无害命令：用来验证取消 / 超时真的杀掉了子进程。"""
    return RuntimeProvisioner(
        manager="test",
        install_argv=["sh", "-c", "echo PID=$$; exec sleep 30"],
    )


def _pid_from(events: list[dict]) -> int:
    for event in events:
        match = re.search(r"PID=(\d+)", event.get("text") or "")
        if match:
            return int(match.group(1))
    raise AssertionError("没有从输出里拿到子进程 PID")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_closing_the_stream_kills_the_child_process():
    """客户端断开 → 生成器被关闭 → ``finally`` 必须杀掉子进程（铁律 10）。"""
    definition = _FakeRuntime()
    definition.provisioner = _sleeper()

    stream = provisioning.stream_provision(definition, "install")
    events = [await stream.__anext__(), await stream.__anext__()]
    assert [event["type"] for event in events] == ["provision.started", "provision.output"]
    pid = _pid_from(events)
    assert _alive(pid), "子进程还没起来，取消用例不成立"

    await stream.aclose()  # 等同于客户端断开时框架关闭事件流

    assert not _alive(pid), "关闭事件流后子进程仍在运行——取消没有生效"


@pytest.mark.asyncio
async def test_timeout_kills_the_child_and_reports_error(monkeypatch: pytest.MonkeyPatch):
    """超过 ``provision_timeout_seconds`` 必须杀掉子进程，并以 finished/error 收尾。"""
    definition = _FakeRuntime()
    definition.provisioner = _sleeper()
    monkeypatch.setattr(
        provisioning,
        "get_settings",
        lambda: SimpleNamespace(provision_timeout_seconds=1),
    )

    events = [event async for event in provisioning.stream_provision(definition, "install")]

    assert events[-1]["type"] == "provision.finished"
    assert events[-1]["ok"] is False and events[-1]["status"] == "error"
    assert any("上限" in (event.get("text") or "") for event in events), "超时应当明确告诉用户"
    assert not _alive(_pid_from(events)), "超时后子进程仍在运行"


# --- 接口 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runtimes_reports_status_and_provisioning(client: httpx.AsyncClient):
    payload = (await client.get("/api/runtimes")).json()
    items = {item["id"]: item for item in payload["items"]}
    assert {"claude-code", "codex", "gemini-cli"} <= set(items)
    claude = items["claude-code"]
    assert claude["provisioning"]["supported"] is True
    assert claude["provisioning"]["manager"] == "npm"
    assert claude["provisioning"]["commands"]["install"]
    assert isinstance(claude["instances"], int)
    # 界面按这个路径渲染官方 Logo，必须随接口一起下发（不能只在前端硬编码）
    assert claude["logo"] == "/icons/kernels/claude.svg"
    assert all(item["logo"] for item in payload["items"])
    assert items["openai-harness"]["provisioning"]["supported"] is False


@pytest.mark.asyncio
async def test_unknown_runtime_is_404(client: httpx.AsyncClient):
    assert (await client.post("/api/runtimes/does-not-exist/install")).status_code == 404
    assert (await client.post("/api/runtimes/does-not-exist/check")).status_code == 404


@pytest.mark.asyncio
async def test_unmanaged_runtime_is_rejected_without_running(client: httpx.AsyncClient):
    response = await client.post("/api/runtimes/custom-cli/install")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_uninstall_requires_confirmation(client: httpx.AsyncClient):
    """未确认的卸载会在建流之前被拒——不会真的执行 npm。"""
    response = await client.post("/api/runtimes/claude-code/uninstall", json={"confirm": False})
    assert response.status_code == 422
    assert "确认" in response.json()["detail"]


@pytest.mark.asyncio
async def test_provision_endpoint_streams_sse(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """整条 SSE 链路：接口把安装器的 provision.* 事件翻成事件流后收尾 stream.closed。"""
    from app.api import runtimes as runtimes_api

    monkeypatch.setattr(runtimes_api, "get_agent", lambda _type_id: _FakeRuntime())
    async with client.stream("POST", "/api/runtimes/fake-runtime/install", json={"confirm": True}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert "event: provision.started" in body
    assert "安装完成" in body
    assert "event: provision.finished" in body
    assert "event: stream.closed" in body


@pytest.mark.asyncio
async def test_check_endpoint_returns_latest(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    from app.api import runtimes as runtimes_api

    monkeypatch.setattr(runtimes_api, "get_agent", lambda _type_id: _FakeRuntime())
    payload = (await client.post("/api/runtimes/fake-runtime/check")).json()
    assert payload["supported"] is True and payload["latest"] == "9.9.9"
