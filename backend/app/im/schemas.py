"""IM 接口的请求 / 响应契约。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ImOptions(BaseModel):
    """这个连接发什么：过程内容默认全部关闭，聊天里只有正文。"""

    show_thinking: bool = False
    show_tools: bool = False
    show_logs: bool = False


class ImConnectionCreate(BaseModel):
    platform: str = Field(min_length=1, max_length=32)
    #: 绑定的智能体：所有私聊都进它
    agent_profile_id: str | None = None
    #: 按 ``PLATFORMS`` 声明的字段提交（密钥字段留空表示没填）
    fields: dict[str, Any] = Field(default_factory=dict)
    options: ImOptions = Field(default_factory=ImOptions)
    enabled: bool = True


class ImConnectionUpdate(BaseModel):
    agent_profile_id: str | None = None
    #: 只提交要改的字段；密钥字段留空＝保留已存的值
    fields: dict[str, Any] | None = None
    #: 只提交要改的开关；没传的键保持原值
    options: ImOptions | None = None
    enabled: bool | None = None


class ImConnectionRead(BaseModel):
    id: str
    platform: str
    platform_name: str
    account_name: str = ""
    account_id: str = ""
    agent_profile_id: str | None = None
    agent_name: str | None = None
    agent_avatar: str | None = None
    enabled: bool = True
    #: 库里的最近状态；连接活着时以运行时为准（见 ``state`` / ``error``）
    state: str = "off"
    error: str | None = None
    #: 已填的密钥字段名（值永不回传）
    secret_fields: list[str] = Field(default_factory=list)
    #: 发什么：思考 / 工具 / 日志
    options: dict[str, bool] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    peers: int = 0
    pending: int = 0
    chats: int = 0


class ImPeerRead(BaseModel):
    id: str
    connection_id: str
    sender_id: str
    sender_name: str = ""
    state: str = "pending"
    chat_key: str = ""
    chat_kind: str = "direct"
    chat_title: str = ""
    created_at: datetime


class ImPairRequest(BaseModel):
    allow: bool = True


class ImChatRead(BaseModel):
    id: str
    connection_id: str
    chat_key: str
    kind: str = "direct"
    title: str = ""
    agent_profile_id: str | None = None
    agent_name: str | None = None
    conversation_id: str | None = None
    updated_at: datetime


class ImChatUpdate(BaseModel):
    #: 群里单独绑定哪个智能体；显式传 null 表示「跟随连接」
    agent_profile_id: str | None = None


__all__ = [
    "ImOptions",
    "ImChatRead",
    "ImChatUpdate",
    "ImConnectionCreate",
    "ImConnectionRead",
    "ImConnectionUpdate",
    "ImPairRequest",
    "ImPeerRead",
]
