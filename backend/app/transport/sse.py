"""Server-Sent Events（SSE）的通用传输封装。

这里只认识「事件字典流」，不知道任何业务概念（对话、协作都一样用），
因此它是传输设施而不是 ``api`` 的一部分：``api/`` 与 ``collab/`` 共用同一份实现，
避免各写一套导致行为漂移。
"""
from __future__ import annotations

import asyncio
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
    """包装事件源，并插入心跳，避免反向代理断开长连接。"""
    iterator = source.__aiter__()
    while True:
        try:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=HEARTBEAT_SECONDS)
        except asyncio.TimeoutError:
            yield format_comment("keep-alive")
            continue
        except StopAsyncIteration:
            break
        yield format_event(event)
    yield format_event({"type": "stream.closed"})
