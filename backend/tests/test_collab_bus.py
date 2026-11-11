"""协作事件总线（`collab/bus.py`）：订阅补发、断线退订与缓冲回收。

守护两件事：
1. 晚到的订阅者要先补发本轮已经产生的事件（断开后的「续看」靠它）；
2. 一轮结束、且已经没人订阅时，必须把该运行的缓冲回收掉 —— 客户端断线后不再有人订阅
   是最常见的路径，回收若只挂在订阅者的 ``finally`` 上就会漏掉，整轮事件（可能含大段
   工具输出）会一直留在内存里。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.collab.bus import RunBus

Event = dict


def _notice(text: str) -> Event:
    return {"type": "collab.notice", "text": text}


async def _wait_until(predicate, *, ticks: int = 500) -> bool:
    for _ in range(ticks):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


async def test_late_subscriber_replays_buffered_events():
    """晚到的订阅者先看到已产生的事件，再接着看实时增量。"""
    bus = RunBus()
    released = asyncio.Event()

    async def turn() -> AsyncIterator[Event]:
        yield _notice("第一步")
        await released.wait()
        yield _notice("第二步")

    bus.start("r1", turn)
    assert await _wait_until(lambda: bus._streams.get("r1") is not None and bus._streams["r1"].events)

    subscription = bus.subscribe("r1")
    assert (await subscription.__anext__())["text"] == "第一步"

    released.set()
    assert (await subscription.__anext__())["text"] == "第二步"
    await subscription.aclose()


async def test_finished_turn_is_pruned_when_nobody_is_watching():
    """断线后无人订阅时，一轮结束必须回收缓冲（回归：此前会常驻到进程重启）。"""
    bus = RunBus()
    gate = asyncio.Event()

    async def turn() -> AsyncIterator[Event]:
        yield _notice("第一步")
        await gate.wait()
        yield _notice("第二步")

    bus.start("r1", turn)
    assert await _wait_until(lambda: bus._streams.get("r1") is not None and bus._streams["r1"].events)

    subscription = bus.subscribe("r1")
    assert (await subscription.__anext__())["text"] == "第一步"
    # 模拟客户端断线：关掉这条订阅，之后不再有人订阅。
    await subscription.aclose()
    assert "r1" in bus._streams, "这一轮还在跑，缓冲必须留着（重连要能补画面）"

    gate.set()
    assert await _wait_until(lambda: "r1" not in bus._streams), "一轮结束后缓冲应当被回收"
