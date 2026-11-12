"""协作运行的实时事件总线：把「谁在跑」与「谁在看」解耦。

一次推进（turn）跑在**独立的**后台任务里，产出的 ``collab.*`` 事件先进入本模块的
每运行缓冲，再转发给所有订阅它的 SSE 连接。这样做有两个直接好处：

* 客户端断开（手机锁屏、切后台、隧道抖动、换网络）只退订，**不再打断推进**；
  重新打开页面时按落库详情续看即可，任何设备也能订阅到当前正在跑的那一步；
* 订阅是「可重入」的：新订阅者先补发本轮已产生的事件，再接着看实时增量，
  因此「点开某个岗位看它的实时对话」对任意设备都成立。

本模块只做 asyncio 编排，不碰数据库、不碰 HTTP；服务重启后内存缓冲自然消失，
残留的运行状态统一由 ``store.recover_interrupted`` 收尾（铁律 9）。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

#: 事件信封（与 ``protocol.py`` 产出的一致），这里只当作不透明的字典转发
Event = dict[str, Any]

#: 每运行缓存的事件上限：订阅者晚到也能补画面，超出就从最早处丢弃
MAX_BUFFERED_EVENTS = 4000


class Subscription:
    """一个订阅者的事件队列。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()

    def offer(self, event: Event) -> None:
        self._queue.put_nowait(event)

    def finish(self) -> None:
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[Event]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class _Stream:
    """一个协作运行的实时缓冲：事件历史 + 当前订阅者 + 推进任务句柄。"""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.subscribers: list[Subscription] = []
        self.task: asyncio.Task[None] | None = None
        self.closed = False

    def publish(self, event: Event) -> None:
        self.events.append(event)
        overflow = len(self.events) - MAX_BUFFERED_EVENTS
        if overflow > 0:
            del self.events[:overflow]
        for subscriber in list(self.subscribers):
            subscriber.offer(event)

    def close(self) -> None:
        self.closed = True
        for subscriber in list(self.subscribers):
            subscriber.finish()
        self.subscribers.clear()


class RunBus:
    """按运行 ID 管理后台推进任务与实时订阅。"""

    def __init__(self) -> None:
        self._streams: dict[str, _Stream] = {}

    def _stream(self, run_id: str) -> _Stream:
        stream = self._streams.get(run_id)
        if stream is None:
            stream = _Stream()
            self._streams[run_id] = stream
        return stream

    def is_running(self, run_id: str) -> bool:
        stream = self._streams.get(run_id)
        return bool(stream and stream.task and not stream.task.done())

    def start(self, run_id: str, turn: Callable[[], AsyncIterator[Event]]) -> None:
        """把一次推进交给后台任务；调用方随后用 :meth:`subscribe` 取事件流。"""
        stream = self._stream(run_id)
        stream.events = []
        stream.closed = False
        task = asyncio.create_task(self._drive(run_id, stream, turn))
        stream.task = task
        # 任务真正结束（finally 里 close 之后）再回收缓冲：在 ``finally`` 里做时 ``task.done()``
        # 还是 False，回收会被跳过；而「断线后无人再订阅」时订阅者的 finally 也不会再触发，
        # 于是一整轮的缓冲就会常驻内存。
        task.add_done_callback(lambda _task: self._prune(run_id, stream))

    async def _drive(self, run_id: str, stream: _Stream, turn: Callable[[], AsyncIterator[Event]]) -> None:
        try:
            async for event in turn():
                stream.publish(event)
        except asyncio.CancelledError:
            # 取消（叫停/关机）交给调用方与启动恢复收尾，这里只负责收尾订阅者
            raise
        except Exception as exc:  # noqa: BLE001 - 铁律 11：显式暴露，绝不静默
            stream.publish(
                {"type": "collab.notice", "level": "error", "text": f"协作推进失败：{exc}"}
            )
        finally:
            stream.close()

    def cancel(self, run_id: str) -> bool:
        stream = self._streams.get(run_id)
        if stream is None or stream.task is None or stream.task.done():
            return False
        stream.task.cancel()
        return True

    async def subscribe(self, run_id: str) -> AsyncIterator[Event]:
        """订阅一个运行的实时事件，直到本轮推进结束。晚到者先补发已产生的事件。"""
        stream = self._streams.get(run_id)
        if stream is None:
            return
        subscriber = Subscription()
        for event in list(stream.events):
            subscriber.offer(event)
        # 没有 await 介入，保证「判断在跑」与「登记订阅」之间不会被推进任务插队
        if stream.task is not None and not stream.task.done():
            stream.subscribers.append(subscriber)
        else:
            subscriber.finish()
        try:
            async for event in subscriber.events():
                yield event
        finally:
            if subscriber in stream.subscribers:
                stream.subscribers.remove(subscriber)
            self._prune(run_id, stream)

    def _prune(self, run_id: str, stream: _Stream) -> None:
        """本轮结束且无人订阅时清掉缓冲，避免内存常驻。"""
        # 期间该运行的缓冲可能已被换成了新的一轮（同一 run 连续追问），别误删新对象。
        if self._streams.get(run_id) is not stream:
            return
        running = stream.task is not None and not stream.task.done()
        if stream.closed and not stream.subscribers and not running:
            self._streams.pop(run_id, None)


bus = RunBus()

#: 入口智能体聊天专用的实时缓冲。与主流程分开，避免聊天消息把工单推进的事件覆盖掉。
chat_bus = RunBus()
