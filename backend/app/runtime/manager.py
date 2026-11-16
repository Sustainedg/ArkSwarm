"""跟踪正在运行的任务，以便列出、取消与回收。"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActiveRun:
    id: str
    conversation_id: str
    agent_name: str
    #: 发起这次运行的对话所属用户；``None`` 是鉴权关闭 / 历史数据，视为管理员可见。
    owner_id: str | None = None
    started_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: Any = None
    finished: bool = False
    status: str = "running"
    #: 后台执行任务（连接断开后仍继续，见 ``runtime.service.start_run``）
    task: asyncio.Task | None = None
    #: 本轮的用户消息 / 助手消息 id：重连的客户端据此知道哪条消息正在生成
    user_message_id: str | None = None
    assistant_message_id: str | None = None
    #: 已产生的事件。新订阅者（刷新页面 / 另一台设备）先从这份缓冲重放，
    #: 再接着收实时事件，因此晚到也能看到完整的一轮输出。
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    ended: bool = False
    #: 本次运行的单次执行时限（秒）：0 表示用全局 ``run_timeout_seconds``。
    #: 协作域通过它把团队预算的「单步限时」传进执行层，覆盖全局默认值。
    timeout_seconds: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "agent_name": self.agent_name,
            "status": self.status,
            "elapsed_seconds": round(time.time() - self.started_at, 1),
            "assistant_message_id": self.assistant_message_id,
            "user_message_id": self.user_message_id,
        }

    def publish(self, event: dict[str, Any]) -> None:
        """把一条事件记进缓冲并广播给当前所有订阅者（没有订阅者也要记）。"""
        if self.ended:
            return
        self.events.append(event)
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def close(self) -> None:
        """运行结束：唤醒所有订阅者，让它们的 SSE 流正常收尾。"""
        if self.ended:
            return
        self.ended = True
        for queue in list(self.subscribers):
            queue.put_nowait(None)
        self.subscribers.clear()

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """订阅这一轮的事件：先重放已有事件，再接收实时事件。

        订阅登记与「快照长度」在同一个同步片段里完成（中间没有 ``await``），
        所以不会漏掉登记期间产生的事件，也不会把同一条事件发两遍。
        """
        queue: asyncio.Queue = asyncio.Queue()
        start = len(self.events)
        live = not self.ended
        if live:
            self.subscribers.add(queue)
        try:
            for event in self.events[:start]:
                yield event
            if not live:
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            self.subscribers.discard(queue)


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, ActiveRun] = {}

    def register(
        self, conversation_id: str, agent_name: str, owner_id: str | None = None
    ) -> ActiveRun:
        run = ActiveRun(
            id=uuid.uuid4().hex[:12],
            conversation_id=conversation_id,
            agent_name=agent_name,
            owner_id=owner_id,
        )
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
            except ProcessLookupError:  # 进程已经退出
                pass
        return True

    def cancel_for_conversation(self, conversation_id: str) -> bool:
        run = self.active_for(conversation_id)
        return self.cancel(run.id) if run else False

    def list(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        """列出运行；``owner_id`` 非空时只列该用户的（管理员传 None 看全部）。"""
        return [
            r.snapshot()
            for r in self._runs.values()
            if owner_id is None or r.owner_id in (None, owner_id)
        ]


registry = RunRegistry()
