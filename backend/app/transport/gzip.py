"""带 SSE 豁免的 gzip 中间件：大 JSON 继续压缩，事件流逐块透传。

为什么 SSE 必须豁免
------------------
starlette 0.41.3 的 ``GZipResponder`` 处理流式响应时，每收到一块
``http.response.body`` 就 ``gzip_file.write(chunk)``，再取 ``gzip_buffer.getvalue()``
当作这一块要下发的字节。问题在于 ``gzip.GzipFile`` 内部用的是 zlib 的
``Z_NO_FLUSH``：在显式 ``flush()`` / ``close()`` 之前，zlib 会把数据攒在自己的内部
缓冲里，不会立刻吐出来。于是中间那些分块的 ``getvalue()`` 基本都是空字节串。

实测（50 个流式分块）：只有 2 个分块非空、合计 50 字节 —— 一个是开头的 gzip 头，
一个是收尾 ``close()`` 后的完整压缩体。也就是说 SSE 的事件、心跳、打字机 delta
全部被攒到响应收尾才一次性下发，所谓「逐块压缩逐块下发」并不存在。

这不是可以靠调参绕开的问题：只要走 gzip 压缩，中间分块就不会有输出。
因此只能让 ``text/event-stream`` 这类响应整体跳过压缩，把消息原样透传给 ASGI 服务器，
由服务器按 chunked 传输逐块写出。

判定时机
--------
豁免只能在 ``http.response.start`` 时决定：响应头（含 ``content-type``）只在这里
出现一次，而响应一旦开始按某种编码下发就无法中途反悔。所以这里先缓存 start 消息，
一旦发现 ``content-type`` 含 ``text/event-stream``，就把整条响应标记为豁免：
start 与后续每一块 body 都直接 ``send`` 出去，不经过 gzip 缓冲。
其余响应（大 JSON、静态产物等）仍交给父类 ``GZipResponder`` 原样压缩。
"""
from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: 需要豁免压缩的媒体类型。SSE 靠逐块到达才有意义，一旦被攒到收尾就失去全部价值。
SSE_MEDIA_TYPE = "text/event-stream"


class _SSEExemptGZipResponder(GZipResponder):
    """在父类压缩逻辑前加一道判断：SSE 响应整条透传，其余照旧压缩。"""

    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int = 9) -> None:
        super().__init__(app, minimum_size, compresslevel)
        self.sse_exempt = False

    async def send_with_gzip(self, message: Message) -> None:
        # 只在 start 时判定一次：content-type 决定了这条响应是否值得压缩。
        if message["type"] == "http.response.start":
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            if SSE_MEDIA_TYPE in content_type.lower():
                self.sse_exempt = True

        if not self.sse_exempt:
            await super().send_with_gzip(message)
            return

        # 豁免分支：start 先缓存，等第一块 body 到来时一起放行，
        # 保证「先发头、再发体」的顺序与正常响应完全一致。
        if message["type"] == "http.response.start":
            self.initial_message = message
            return
        if not self.started:
            self.started = True
            await self.send(self.initial_message)
        await self.send(message)


class SSEExemptGZipMiddleware(GZipMiddleware):
    """gzip 中间件，但对 ``text/event-stream`` 响应整体豁免。

    小响应（低于 ``minimum_size``）和 SSE 一样不压缩；差别只在于 SSE 无论多大
    都不压缩，因为它的价值在于「逐块到达」而不是「体积更小」。
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if "gzip" in headers.get("accept-encoding", ""):
                responder = _SSEExemptGZipResponder(
                    self.app, self.minimum_size, compresslevel=self.compresslevel
                )
                await responder(scope, receive, send)
                return
        await self.app(scope, receive, send)
