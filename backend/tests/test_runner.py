"""执行层：子进程输出按行读取的边界用例。

守护历史上真实发生过的故障：内核把整段代码放进一行 JSONL，``StreamReader.readline()``
在超过默认 64 KiB 缓冲上限时抛 ``LimitOverrunError``，整轮运行被误判成 error。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.agents.base import AgentContext, AgentDefinition, Capabilities, Invocation, Runtime
from app.agents.events import log, text_delta
from app.runtime import runner
from app.runtime.manager import registry
from app.runtime.runner import _StreamLines
from app.settings import get_settings

FAKE_LONG_LINE_AGENT = str(Path(__file__).parent / "fakes" / "fake_long_line_agent.py")
FAKE_LURKING_CHILD = str(Path(__file__).parent / "fakes" / "fake_lurking_child.py")
FAKE_RPC_CHILD = str(Path(__file__).parent / "fakes" / "fake_rpc_child.py")
FAKE_DEAF_CHILD = str(Path(__file__).parent / "fakes" / "fake_deaf_child.py")


async def test_readline_default_limit_rejects_a_long_line():
    """先固定住「默认 readline 会炸」这个前提，修复一旦失效测试立刻变红。"""
    payload = b"x" * 100_000
    reader = asyncio.StreamReader()
    reader.feed_data(payload + b"\n")
    reader.feed_eof()
    # StreamReader.readline 把内部的 LimitOverrunError 包装成 ValueError 抛给调用方。
    with pytest.raises(ValueError, match="chunk is longer than limit"):
        await reader.readline()


async def test_stream_lines_returns_a_line_far_beyond_the_default_limit():
    payload = b"x" * 300_000
    reader = asyncio.StreamReader()
    reader.feed_data(payload + b"\n")
    reader.feed_eof()

    lines = _StreamLines(reader)
    assert await lines.readline() == payload
    assert lines.overflowed is False
    assert await lines.readline() is None


async def test_stream_lines_keeps_a_trailing_line_without_newline():
    reader = asyncio.StreamReader()
    reader.feed_data(b"first\nsecond")
    reader.feed_eof()

    lines = _StreamLines(reader)
    assert await lines.readline() == b"first"
    assert await lines.readline() == b"second"
    assert await lines.readline() is None


async def test_stream_lines_drops_content_beyond_the_memory_cap(monkeypatch):
    """越界时只丢这一行，后续行照常读出，并置位 ``overflowed`` 供调用方显式告知用户。"""
    monkeypatch.setattr(runner, "_MAX_LINE_BYTES", 16)
    monkeypatch.setattr(runner, "_STREAM_CHUNK_SIZE", 4)
    reader = asyncio.StreamReader()
    reader.feed_data(b"ABCDEFGHIJKLMNOPQRST\nnext\n")
    reader.feed_eof()

    lines = _StreamLines(reader)
    assert await lines.readline() == b"next"
    assert lines.overflowed is True
    assert await lines.readline() is None


async def _read_sse(response: httpx.Response) -> list[dict]:
    events: list[dict] = []
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


async def test_cli_run_survives_a_very_long_output_line(client: httpx.AsyncClient):
    """端到端：真实 CLI 输出超长单行时，整轮运行必须完整落库、不得产出 error 事件。"""
    role = (
        await client.post(
            "/api/agents",
            json={
                "name": "长行 CLI",
                "type_id": "custom-cli",
                "model": "fake-model",
                "options": {
                    "command": sys.executable,
                    "args": [FAKE_LONG_LINE_AGENT],
                    "prompt_mode": "stdin",
                    "output_format": "jsonl",
                    "text_path": "delta",
                    "delta_mode": True,
                },
            },
        )
    ).json()
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "写个系统"}
    ) as response:
        assert response.status_code == 200
        events = await _read_sse(response)

    assert [e["type"] for e in events][-1] == "stream.closed"
    assert not [e for e in events if e["type"] == "error"]
    deltas = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert deltas == "A" * 200_000 + "|尾巴"

    detail = (await client.get(f"/api/conversations/{conversation['id']}")).json()
    message = detail["messages"][-1]
    assert message["status"] == "complete"
    assert message["error"] is None
    assert message["content"] == deltas
    assert message["usage"]["output_tokens"] == 7


async def test_cli_run_ends_even_if_a_grandchild_keeps_the_pipe_open(client: httpx.AsyncClient):
    """内核退出后，后台孙进程还握着 stdout（Codex 的 ``git ls-remote`` 就是这种）。

    执行层不能为了等 EOF 把整轮运行拖住：子进程一退出，只再宽限几秒把残余读完就收工。
    这里用「睡 20 秒」的孙进程固定住这个场景，要求整轮远早于它结束。
    """
    role = (
        await client.post(
            "/api/agents",
            json={
                "name": "留后手的 CLI",
                "type_id": "custom-cli",
                "model": "fake-model",
                "options": {
                    "command": sys.executable,
                    "args": [FAKE_LURKING_CHILD],
                    "prompt_mode": "stdin",
                    "output_format": "jsonl",
                    "text_path": "delta",
                    "delta_mode": True,
                },
            },
        )
    ).json()
    conversation = (await client.post("/api/conversations", json={"agent_profile_id": role["id"]})).json()

    started = time.monotonic()
    async with client.stream(
        "POST", f"/api/conversations/{conversation['id']}/messages", json={"content": "你好"}
    ) as response:
        assert response.status_code == 200
        events = await _read_sse(response)
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"整轮运行被后台孙进程拖住了：{elapsed:.1f} 秒"
    assert [e["type"] for e in events][-1] == "stream.closed"
    assert not [e for e in events if e["type"] == "error"]
    deltas = "".join(e.get("text") or "" for e in events if e["type"] == "message.delta")
    assert deltas == "done"


class _RpcProbeAgent(AgentDefinition):
    """最小双向内核：读完第一帧再排队发第二帧，收到 ``done`` 就声明收工。"""

    id = "probe-rpc"
    name = "双向探针"
    runtime = Runtime.CLI
    executable = sys.executable
    capabilities = Capabilities(sessions=True)

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        return Invocation(
            argv=[sys.executable, FAKE_RPC_CHILD],
            stdin=json.dumps({"id": 1}) + "\n",
            stdin_hold=True,
        )

    def parse_line(self, line: str, run):
        payload = json.loads(line)
        if payload.get("seq"):
            yield log(f"echo:{payload['seq']}")
            if payload["seq"] == 1:
                # 适配器只生产数据；真正写回 stdin 的是执行层。
                run.send(json.dumps({"id": 2}) + "\n")
        if payload.get("done"):
            run.mark_done()


async def _drain(stream) -> list:
    return [event async for event in stream]


async def test_cli_run_writes_adapter_requests_back_to_stdin():
    """常驻 JSON-RPC 内核（Codex app-server 那类）靠 stdin 逐步收请求。

    执行层必须：① ``stdin_hold`` 时不急着关闭 stdin；② 把适配器排队的请求照写回内核；
    ③ 适配器 ``mark_done()`` 后立刻关 stdin 收工，而不是干等内核自己 EOF。
    """
    definition = _RpcProbeAgent()
    context = AgentContext(agent_id="a1", agent_name="probe", prompt="你好")
    active = registry.register("conversation-probe", "probe")
    started = time.monotonic()
    try:
        events = await asyncio.wait_for(
            _drain(runner.stream_run(definition, context, active)), timeout=20
        )
    finally:
        registry.finish(active, "ok")
    elapsed = time.monotonic() - started

    kinds = [event.type.value for event in events]
    # 第二帧是适配器排队、执行层代写的；内核回了两声 echo 才可能收工。
    assert kinds.count("log") >= 2, kinds
    assert kinds[-1] == "run.finished"
    assert elapsed < 15, f"双向内核没有被及时收尾：{elapsed:.1f} 秒"
    assert active.process is not None and active.process.returncode == 0


class _DeafProbeAgent(AgentDefinition):
    """把一个远超管道缓冲的请求投给一个「完全不读 stdin」的内核。"""

    id = "probe-deaf"
    name = "不读 stdin 的探针"
    runtime = Runtime.CLI
    executable = sys.executable

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        # 300 KiB 远大于管道缓冲：内核不读的话 drain() 必然卡住。
        return Invocation(
            argv=[sys.executable, FAKE_DEAF_CHILD],
            stdin=json.dumps({"blob": "x" * 300_000}) + "\n",
            stdin_hold=True,
        )

    def parse_line(self, line: str, run):
        payload = json.loads(line)
        if payload.get("delta"):
            yield text_delta(payload["delta"])


async def test_cli_run_does_not_hang_when_the_kernel_stops_reading_stdin(monkeypatch):
    """内核不读 stdin 时 ``drain()`` 会一直堵着：写 stdin 必须有上限（铁律 10）。"""
    monkeypatch.setattr(runner, "_STDIN_DRAIN_SECONDS", 0.3)
    monkeypatch.setattr(runner, "_STREAM_POLL_SECONDS", 0.2)
    monkeypatch.setattr(runner, "_EXIT_DRAIN_SECONDS", 0.2)
    monkeypatch.setattr(runner, "_EXIT_WAIT_SECONDS", 2.0)

    definition = _DeafProbeAgent()
    context = AgentContext(agent_id="a1", agent_name="deaf", prompt="你好")
    active = registry.register("conversation-deaf", "deaf")
    started = time.monotonic()
    try:
        events = await asyncio.wait_for(
            _drain(runner.stream_run(definition, context, active)), timeout=20
        )
    finally:
        registry.finish(active, "ok")
    elapsed = time.monotonic() - started

    assert [event.type.value for event in events][-1] == "run.finished"
    assert elapsed < 15, f"写 stdin 卡住后整轮运行没有收尾：{elapsed:.1f} 秒"
    assert any(event.text == "start" for event in events)


FAKE_SLEEP_AGENT = str(Path(__file__).parent / "fakes" / "fake_sleep_agent.py")


class _SleepProbeAgent(AgentDefinition):
    """跑得足够久的 CLI 内核，用来验证「单步限时」的掐表。"""

    id = "probe-sleep"
    name = "长跑探针"
    runtime = Runtime.CLI
    executable = sys.executable
    #: 内核要跑的秒数（负数用例改短，免得整轮测试拖太久）
    seconds = "30"

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        return Invocation(argv=[sys.executable, FAKE_SLEEP_AGENT, self.seconds])

    def parse_line(self, line: str, run):
        payload = json.loads(line)
        if payload.get("delta"):
            yield text_delta(payload["delta"])


async def test_stream_run_honors_per_run_timeout_override(monkeypatch):
    """handle.timeout_seconds（协作域的单步限时）必须覆盖全局默认并掐死长跑内核。"""
    monkeypatch.setattr(runner, "_STREAM_POLL_SECONDS", 0.2)

    definition = _SleepProbeAgent()
    context = AgentContext(agent_id="a1", agent_name="sleep", prompt="慢慢干")
    active = registry.register("conversation-sleep", "sleep")
    active.timeout_seconds = 2  # 内核要跑 30 秒，这里 2 秒掐死
    started = time.monotonic()
    try:
        events = await asyncio.wait_for(
            _drain(runner.stream_run(definition, context, active)), timeout=30
        )
    finally:
        registry.finish(active, "ok")
    elapsed = time.monotonic() - started

    kinds = [event.type.value for event in events]
    assert kinds[-1] == "run.finished"
    assert any(
        event.type.value == "error" and "2 秒上限" in (event.text or "")
        for event in events
    ), [event.type.value for event in events]
    assert elapsed < 20, f"单步限时没有掐死长跑内核：{elapsed:.1f} 秒"


async def test_stream_run_per_run_timeout_negative_means_no_limit(monkeypatch):
    """单步限时设为「不限」（负数）时，超时路径必须整体失效。"""
    monkeypatch.setattr(runner, "_STREAM_POLL_SECONDS", 0.2)

    definition = _SleepProbeAgent()
    definition.seconds = "4"  # 不限时：内核自己跑完收工
    context = AgentContext(agent_id="a1", agent_name="sleep", prompt="慢慢干")
    active = registry.register("conversation-sleep-neg", "sleep")
    active.timeout_seconds = -1  # 不限
    try:
        events = await asyncio.wait_for(
            _drain(runner.stream_run(definition, context, active)), timeout=30
        )
    finally:
        registry.finish(active, "ok")

    kinds = [event.type.value for event in events]
    assert kinds[-1] == "run.finished"
    assert not [event for event in events if event.type.value == "error"], kinds


def test_plain_chat_is_unlimited_by_default(monkeypatch):
    """普通对话没指定时限、全局也没配：不得有任何 deadline，跑多久都不该被掐断。

    这条守着「3600 秒默认值」的回归：对话域的 ``ActiveRun.timeout_seconds`` 恒为 0，
    以前它会回落到 ``settings.run_timeout_seconds = 3600``，于是普通对话一小时后被杀。
    """
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", None)
    active = registry.register("conversation-plain", "plain")
    try:
        assert active.timeout_seconds == 0
        assert runner.resolve_timeout_limit(active) is None
    finally:
        registry.finish(active, "ok")


def test_configured_global_timeout_still_guards_plain_chat(monkeypatch):
    """部署方显式配了护栏时，普通对话仍按它掐表——只是默认值不再是 3600。"""
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 42)
    active = registry.register("conversation-guarded", "guarded")
    try:
        assert runner.resolve_timeout_limit(active) == 42
    finally:
        registry.finish(active, "ok")


def test_collab_step_timeout_wins_over_global_default(monkeypatch):
    """协作域的单步限时优先于全局值：配了对话护栏也不该影响协作的「不限」。"""
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 42)
    active = registry.register("collab-unlimited", "collab")
    active.timeout_seconds = -1
    try:
        assert runner.resolve_timeout_limit(active) is None
    finally:
        registry.finish(active, "ok")

