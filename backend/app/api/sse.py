"""Server-sent-events plumbing."""
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
    """Wrap an event source, adding heartbeats so proxies keep the socket open."""
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
