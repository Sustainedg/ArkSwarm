"""执行层：子进程输出按行读取的边界用例。

守护历史上真实发生过的故障：内核把整段代码放进一行 JSONL，``StreamReader.readline()``
在超过默认 64 KiB 缓冲上限时抛 ``LimitOverrunError``，整轮运行被误判成 error。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from app.runtime import runner
from app.runtime.runner import _StreamLines

FAKE_LONG_LINE_AGENT = str(Path(__file__).parent / "fakes" / "fake_long_line_agent.py")


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
