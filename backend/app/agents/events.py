"""Normalized event protocol shared by every agent adapter.

Everything an adapter emits is expressed with these types, so the transport
layer (SSE), the persistence layer and the UI never need to know whether the
run came from a CLI subprocess (Claude Code, Codex, Gemini CLI, ...) or from an
in-process API harness (DeepSeek, or any OpenAI-compatible endpoint).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    RUN_STARTED = "run.started"
    MESSAGE_DELTA = "message.delta"
    THINKING_DELTA = "thinking.delta"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    USAGE = "usage"
    SESSION = "session"
    LOG = "log"
    ERROR = "error"
    RUN_FINISHED = "run.finished"


@dataclass(slots=True)
class AgentEvent:
    type: EventType
    text: str | None = None
    name: str | None = None
    call_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = self.type.value
        data["payload"] = self.payload or {}
        return {k: v for k, v in data.items() if v is not None or k == "payload"}


# --- small constructor helpers, keeps adapters terse -------------------------


def text_delta(text: str) -> AgentEvent:
    return AgentEvent(EventType.MESSAGE_DELTA, text=text)


def thinking_delta(text: str) -> AgentEvent:
    return AgentEvent(EventType.THINKING_DELTA, text=text)


def tool_call(name: str, call_id: str | None = None, **input_: Any) -> AgentEvent:
    return AgentEvent(EventType.TOOL_CALL, name=name, call_id=call_id, payload={"input": input_})


def tool_result(call_id: str | None, output: str, is_error: bool = False) -> AgentEvent:
    return AgentEvent(
        EventType.TOOL_RESULT,
        call_id=call_id,
        payload={"output": output, "is_error": is_error},
    )


def usage(**counters: Any) -> AgentEvent:
    return AgentEvent(EventType.USAGE, payload=counters)


def session(session_id: str) -> AgentEvent:
    return AgentEvent(EventType.SESSION, payload={"session_id": session_id})


def log(message: str, level: str = "info") -> AgentEvent:
    return AgentEvent(EventType.LOG, text=message, payload={"level": level})


def error(message: str, **extra: Any) -> AgentEvent:
    return AgentEvent(EventType.ERROR, text=message, payload=extra)


def run_started(run_id: str) -> AgentEvent:
    return AgentEvent(EventType.RUN_STARTED, payload={"run_id": run_id})


def run_finished(status: str = "ok", **extra: Any) -> AgentEvent:
    return AgentEvent(EventType.RUN_FINISHED, payload={"status": status, **extra})
