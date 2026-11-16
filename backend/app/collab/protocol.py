"""协作域的**传输信封**协议（与智能体协议分属两层，互不干扰）。

* 第一层：``agents/events.py`` 的 10 种规范化事件 —— 适配器协议，本方案一字未改。
* 第二层：这里的 ``collab.*`` —— 协作层把「某一步的原始事件」包进信封，再补上
  工单流转、运行状态等只有协作才有的元事件。前端协作页面消费这一层。

之所以另起一层而不是往 ``EventType`` 里加成员：铁律 5 约束的是适配器协议，
协作是**消费者**，不该反过来改造协议本身。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..agents.events import AgentEvent


class CollabEventType(str, Enum):
    RUN_STARTED = "collab.run.started"
    STEP_STARTED = "collab.step.started"
    #: 内嵌一条原始 AgentEvent，供前端逐字渲染
    STEP_EVENT = "collab.step.event"
    STEP_FINISHED = "collab.step.finished"
    TICKET_CREATED = "collab.ticket.created"
    TICKET_UPDATED = "collab.ticket.updated"
    RUN_STATUS = "collab.run.status"
    NOTICE = "collab.notice"
    #: 入口智能体聊天通道：与工单步骤分开，客户端用独立的归约器渲染
    CHAT_STARTED = "collab.chat.started"
    CHAT_EVENT = "collab.chat.event"
    CHAT_FINISHED = "collab.chat.finished"


@dataclass(slots=True)
class CollabEvent:
    type: CollabEventType
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, **self.payload}


def run_started(run_id: str, team_id: str, title: str, entry_key: str | None) -> CollabEvent:
    return CollabEvent(
        CollabEventType.RUN_STARTED,
        {"run_id": run_id, "team_id": team_id, "title": title, "entry_key": entry_key},
    )


def step_started(step_id: str, run_id: str, ticket_id: str | None, node_key: str, duty: str, agent_name: str) -> CollabEvent:
    return CollabEvent(
        CollabEventType.STEP_STARTED,
        {
            "step_id": step_id,
            "run_id": run_id,
            "ticket_id": ticket_id,
            "node_key": node_key,
            "duty": duty,
            "agent_name": agent_name,
        },
    )


def step_event(step_id: str, event: AgentEvent) -> CollabEvent:
    """把一步里的原始智能体事件原样透传（前端复用同一个归约器）。"""
    return CollabEvent(CollabEventType.STEP_EVENT, {"step_id": step_id, "event": event.to_dict()})


def step_finished(step_id: str, status: str, action: str = "", usage: dict[str, Any] | None = None, error: str | None = None) -> CollabEvent:
    return CollabEvent(
        CollabEventType.STEP_FINISHED,
        {"step_id": step_id, "status": status, "action": action, "usage": usage or {}, "error": error},
    )


def ticket_created(ticket: dict[str, Any]) -> CollabEvent:
    return CollabEvent(CollabEventType.TICKET_CREATED, {"ticket": ticket})


def ticket_updated(ticket: dict[str, Any]) -> CollabEvent:
    return CollabEvent(CollabEventType.TICKET_UPDATED, {"ticket": ticket})


def run_status(status: str, reason: str | None = None, **extra: Any) -> CollabEvent:
    return CollabEvent(CollabEventType.RUN_STATUS, {"status": status, "reason": reason, **extra})


def notice(text: str, level: str = "info", **extra: Any) -> CollabEvent:
    return CollabEvent(CollabEventType.NOTICE, {"text": text, "level": level, **extra})


def chat_started(
    message_id: str, run_id: str, agent_name: str, role_key: str = ""
) -> CollabEvent:
    return CollabEvent(
        CollabEventType.CHAT_STARTED,
        {
            "message_id": message_id,
            "run_id": run_id,
            "agent_name": agent_name,
            "role_key": role_key,
        },
    )


def chat_event(message_id: str, event: AgentEvent) -> CollabEvent:
    """把聊天这一轮里的原始智能体事件透传；前端复用同一个归约器逐字渲染。"""
    return CollabEvent(CollabEventType.CHAT_EVENT, {"message_id": message_id, "event": event.to_dict()})


def chat_finished(
    message_id: str,
    status: str,
    *,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
    warning: str | None = None,
    applied: dict[str, Any] | None = None,
    advance: dict[str, Any] | None = None,
) -> CollabEvent:
    """聊天收尾；``advance`` 非 None 表示 CEO 判断这条消息需要推进任务。

    它是给用户渲染「推进任务决策卡」的数据：{reason, order, options}；
    用户点选后由前端把 order（或所选选项的 value）作为消息发进工单通道。
    """
    return CollabEvent(
        CollabEventType.CHAT_FINISHED,
        {
            "message_id": message_id,
            "status": status,
            "usage": usage or {},
            "error": error,
            "warning": warning,
            "applied": applied or {},
            "advance": advance,
        },
    )
