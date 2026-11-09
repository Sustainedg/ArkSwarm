"""Tracks in-flight runs so they can be listed, cancelled and reaped."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActiveRun:
    id: str
    conversation_id: str
    agent_name: str
    started_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: Any = None
    finished: bool = False
    status: str = "running"

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "agent_name": self.agent_name,
            "status": self.status,
            "elapsed_seconds": round(time.time() - self.started_at, 1),
        }


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, ActiveRun] = {}

    def register(self, conversation_id: str, agent_name: str) -> ActiveRun:
        run = ActiveRun(id=uuid.uuid4().hex[:12], conversation_id=conversation_id, agent_name=agent_name)
        self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> ActiveRun | None:
        return self._runs.get(run_id)

    def active_for(self, conversation_id: str) -> ActiveRun | None:
        for run in self._runs.values():
            if run.conversation_id == conversation_id and not run.finished:
                return run
        return None

    def finish(self, run: ActiveRun, status: str = "ok") -> None:
        run.finished = True
        run.status = status
        self._runs.pop(run.id, None)

    def cancel(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run:
            return False
        run.cancel_event.set()
        process = run.process
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:  # already gone
                pass
        return True

    def cancel_for_conversation(self, conversation_id: str) -> bool:
        run = self.active_for(conversation_id)
        return self.cancel(run.id) if run else False

    def list(self) -> list[dict[str, Any]]:
        return [r.snapshot() for r in self._runs.values()]


registry = RunRegistry()
