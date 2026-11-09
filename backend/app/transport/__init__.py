"""通用传输设施：与具体业务无关的协议封装（当前只有 SSE）。"""
from .sse import format_comment, format_event, sse_stream  # noqa: F401

__all__ = ["format_comment", "format_event", "sse_stream"]
