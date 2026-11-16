"""gzip 中间件对 SSE 的豁免行为。

守护两类真实故障：

* **SSE 被压缩缓冲**：starlette 的 ``GZipResponder`` 用 zlib 的 ``Z_NO_FLUSH`` 写流，
  中间分块的 ``getvalue()`` 是空的，事件、心跳与打字机 delta 全部被攒到响应收尾，
  界面表现就是「整轮运行结束才蹦出全文」。``SSEExemptGZipMiddleware`` 必须让流逐块到达。
* **豁免过头**：普通大 JSON 仍要被压缩，否则白白丢掉带宽收益。

为什么不用 starlette 的 ``TestClient``：它把整条响应收进 ``io.BytesIO``，应用跑完才把
字节交给客户端，客户端根本观察不到「分块到达时刻」—— 拿它写时间断言等于没测（旧行为
同样会通过）。所以这里用进程内 uvicorn 监听临时端口，httpx 流式读取真实到达时间。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.transport.gzip import SSEExemptGZipMiddleware

#: 每块都超过中间件的 minimum_size，确保走的是「流式」分支而不是整体压缩分支。
CHUNK_SIZE = 1500
CHUNK_COUNT = 10
CHUNK_INTERVAL = 0.08


async def _sse_endpoint(request: Request) -> StreamingResponse:
    async def stream():
        for index in range(CHUNK_COUNT):
            payload = "x" * CHUNK_SIZE
            yield f"event: chunk\ndata: {index}:{payload}\n\n"
            # 真实的打字机 delta / 心跳也是这种「有间隔地一块块产出」。
            await asyncio.sleep(CHUNK_INTERVAL)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _json_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"items": list(range(600))})


@dataclass
class _StreamResult:
    """一次流式请求的观测结果：每块到达的相对时刻（秒）与整条流的收尾时刻。"""

    status_code: int
    encoding: str | None
    arrivals: list[float]
    finished: float
    body: bytes


@contextlib.contextmanager
def _serve(app: Starlette) -> Iterator[str]:
    """在后台线程里起一个监听临时端口的 uvicorn，返回可访问的基地址。"""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn 未能在 10 秒内启动")
        time.sleep(0.02)
    try:
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("uvicorn 未能在 10 秒内退出")


async def _read_stream(url: str) -> _StreamResult:
    """流式读取原始字节，记录每个非空分块的到达时刻。"""
    started = time.monotonic()
    arrivals: list[float] = []
    body = bytearray()
    async with httpx.AsyncClient(timeout=15) as client:
        async with client.stream("GET", url, headers={"Accept-Encoding": "gzip"}) as response:
            status_code = response.status_code
            encoding = response.headers.get("content-encoding")
            async for chunk in response.aiter_raw():
                if chunk:
                    arrivals.append(time.monotonic() - started)
                    body.extend(chunk)
    return _StreamResult(
        status_code=status_code,
        encoding=encoding,
        arrivals=arrivals,
        finished=time.monotonic() - started,
        body=bytes(body),
    )


def _build_app(middleware: Any, **kwargs: Any) -> Starlette:
    app = Starlette(routes=[Route("/events", _sse_endpoint), Route("/json", _json_endpoint)])
    app.add_middleware(middleware, **kwargs)
    return app


async def test_sse_is_delivered_chunk_by_chunk():
    """SSE 必须逐块到达：至少 5 块在流收尾之前就送达客户端，且响应不声明 gzip。"""
    app = _build_app(SSEExemptGZipMiddleware, minimum_size=1024, compresslevel=6)
    with _serve(app) as base:
        result = await _read_stream(f"{base}/events")

    assert result.status_code == 200
    assert result.encoding is None, "SSE 响应不应带 content-encoding"

    # 被 gzip 缓冲时只有开头 gzip 头和收尾压缩体两块；正常豁免时十块全部到达。
    assert len(result.arrivals) >= 5, f"只收到 {len(result.arrivals)} 个非空分块：{result.arrivals}"

    # 关键断言：多块在「流收尾」之前很久就到达。缓冲实现下这些块全挤在 finished 附近。
    # 只用相对收尾时刻的度量，不用绝对时钟：冷启动/机器负载会让首块的整体时间漂移。
    early = [moment for moment in result.arrivals if moment < result.finished - 0.3]
    assert len(early) >= 5, f"没有分块在收尾前到达：{result.arrivals}（收尾于 {result.finished:.2f}s）"
    # 到达时刻确实被拉开（10 块 × 0.08s），不是收尾时一次性灌进来的。
    assert result.arrivals[-1] - result.arrivals[0] > 0.4, f"分块挤在一起：{result.arrivals}"

    # 内容原样透传，没有被压缩成二进制垃圾。
    for index in (0, CHUNK_COUNT - 1):
        assert f"data: {index}:".encode() in result.body
    assert result.body.count(b"event: chunk") == CHUNK_COUNT


async def test_large_json_response_is_still_compressed():
    """对照：普通大 JSON 仍走 gzip，压缩收益不能因豁免而丢掉。"""
    app = _build_app(SSEExemptGZipMiddleware, minimum_size=1024, compresslevel=6)
    with _serve(app) as base:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{base}/json", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    payload = json.dumps({"items": list(range(600))})
    assert len(payload) > 1024, "样本必须超过 minimum_size，否则压缩断言没有意义"
    assert response.json()["items"] == list(range(600))


async def test_plain_gzip_middleware_does_buffer_sse():
    """对照：未豁免的原始 GZipMiddleware 确实会把 SSE 攒到收尾 —— 证明上面的测试有区分力。"""
    app = _build_app(GZipMiddleware, minimum_size=1024, compresslevel=6)
    with _serve(app) as base:
        result = await _read_stream(f"{base}/events")

    assert result.status_code == 200
    assert result.encoding == "gzip"

    # 提前到达的最多只有开头那 10 字节左右的 gzip 头，正文全在响应收尾才吐出来。
    early = [moment for moment in result.arrivals if moment < result.finished - 0.3]
    assert len(early) <= 1, f"原始 gzip 中间件竟然逐块下发了：{result.arrivals}"
