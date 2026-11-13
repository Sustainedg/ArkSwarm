"""SSE 传输层：心跳只发注释，不能把事件源（一次运行）掐断。"""
from __future__ import annotations

import asyncio

import pytest

from app.transport import sse


def _event_lines(chunks: list[str]) -> list[str]:
    return [chunk for chunk in chunks if chunk.startswith("event: ")]


async def test_heartbeat_keeps_a_slow_source_alive(monkeypatch):
    """两次事件间隔超过心跳时，只是一行 keep-alive，后续事件必须照常送达。

    回归：旧实现用 ``asyncio.wait_for(iterator.__anext__(), timeout)``，超时会取消并
    关闭事件源 —— Hermes 初始化后要等模型整段回答才出文本，超过 15 秒就会被静默掐断，
    用户看到空回复、数据库里却是 complete。
    """
    monkeypatch.setattr(sse, "HEARTBEAT_SECONDS", 0.05)
    closed: list[bool] = []

    async def source():
        try:
            yield {"type": "a"}
            await asyncio.sleep(0.2)  # 远大于心跳间隔
            yield {"type": "b"}
            yield {"type": "run.finished"}
        finally:
            closed.append(True)

    chunks = [chunk async for chunk in sse.sse_stream(source())]

    assert ": keep-alive\n\n" in chunks
    assert _event_lines(chunks) == [
        'event: a\ndata: {"type": "a"}\n\n',
        'event: b\ndata: {"type": "b"}\n\n',
        'event: run.finished\ndata: {"type": "run.finished"}\n\n',
        'event: stream.closed\ndata: {"type": "stream.closed"}\n\n',
    ]
    # 事件源自然结束后才收尾
    assert closed == [True]


async def test_source_errors_are_not_swallowed():
    """事件源抛错要原样冒泡，不能因为包了一层 SSE 就静默吞掉。"""

    async def source():
        yield {"type": "a"}
        raise ValueError("内核输出坏了")

    with pytest.raises(ValueError, match="内核输出坏了"):
        [chunk async for chunk in sse.sse_stream(source())]


async def test_closing_the_stream_closes_the_source(monkeypatch):
    """客户端断开时，事件源必须立刻被关闭（运行的 finally / 取消逻辑不能等 GC）。"""
    monkeypatch.setattr(sse, "HEARTBEAT_SECONDS", 10)
    closed: list[bool] = []

    async def source():
        try:
            yield {"type": "a"}
            await asyncio.sleep(30)
            yield {"type": "b"}
        finally:
            closed.append(True)

    stream = sse.sse_stream(source())
    first = await stream.__anext__()
    assert first.startswith("event: a")
    await stream.aclose()
    assert closed == [True]