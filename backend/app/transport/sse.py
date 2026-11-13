"""Server-Sent Events（SSE）的通用传输封装。

这里只认识「事件字典流」，不知道任何业务概念（对话、协作都一样用），
因此它是传输设施而不是 ``api`` 的一部分：``api/`` 与 ``collab/`` 共用同一份实现，
避免各写一套导致行为漂移。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

HEARTBEAT_SECONDS = 15


def format_event(event: dict[str, Any]) -> str:
    kind = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {kind}\ndata: {payload}\n\n"


def format_comment(text: str) -> str:
    return f": {text}\n\n"


async def sse_stream(source: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    """包装事件源，并插入心跳，避免反向代理断开长连接。

    心跳**不能打断事件源**：``asyncio.wait_for(iterator.__anext__(), timeout)`` 超时会
    取消正在运行的 ``__anext__``，进而关闭整条事件源（运行的 ``finally`` 随之执行）。
    内核在两次事件之间沉默超过心跳间隔时（Hermes 初始化后要等模型整段回答才出第一个
    文本事件），整轮运行就会被静默掐断：界面看到的是空回复，数据库里还标成 complete。

    因此这里改为「在同一个待取任务上等待超时」：超时只发一行注释，不取消任务，
    下一次循环继续等它；只有消费者（客户端断开 / 响应被取消）才会在 ``finally`` 里
    取消待取任务并关闭事件源。
    """
    iterator = source.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield format_comment("keep-alive")
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                break
            yield format_event(event)
            # 上一个事件已经写给客户端，再取下一个（保持背压，不抢跑事件源）。
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        if not pending.done():
            pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await pending
        # 消费者提前收工（客户端断开 / 响应被取消）时，事件源也必须关掉，
        # 否则运行的清理逻辑要等垃圾回收才执行，取消信号会被拖住。
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await close()
    yield format_event({"type": "stream.closed"})
