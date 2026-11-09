"""HTTP 接口的请求体与响应体模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type_id: str
    description: str | None = None
    gateway_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    gateway_url: str | None = None
    model: str | None = None
    #: 省略表示保留已存密钥，传 "" 表示清除
    api_key: str | None = None
    options: dict[str, Any] | None = None
    enabled: bool | None = None


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type_id: str
    description: str | None = None
    gateway_url: str | None = None
    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    created_at: datetime
    updated_at: datetime
    #: 派生字段
    type_name: str | None = None
    runtime: str | None = None
    api_key_set: bool = False
    api_key_preview: str | None = None
    availability: dict[str, Any] | None = None


class AgentTestResult(BaseModel):
    ok: bool
    message: str
    checks: list[dict[str, Any]] = Field(default_factory=list)


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    agent_id: str
    avatar: str = "🛠️"
    tagline: str | None = None
    system_prompt: str = ""
    workspace_dir: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class RoleUpdate(BaseModel):
    name: str | None = None
    agent_id: str | None = None
    avatar: str | None = None
    tagline: str | None = None
    system_prompt: str | None = None
    workspace_dir: str | None = None
    parameters: dict[str, Any] | None = None


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    avatar: str
    tagline: str | None = None
    system_prompt: str
    agent_id: str
    workspace_dir: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    agent_name: str | None = None
    agent_type_id: str | None = None


class ConversationCreate(BaseModel):
    title: str | None = None
    role_id: str
    workspace_dir: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    archived: bool | None = None


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    role_id: str
    agent_id: str
    session_id: str | None = None
    workspace_dir: str | None = None
    archived: bool
    created_at: datetime
    updated_at: datetime
    role_name: str | None = None
    role_avatar: str | None = None
    agent_name: str | None = None
    message_count: int = 0


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str
    parts: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    status: str
    error: str | None = None
    created_at: datetime


class SendMessage(BaseModel):
    content: str = Field(min_length=1)
    #: 可选的单轮覆盖项
    workspace_dir: str | None = None
    reset_session: bool = False

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value


class ConversationDetail(BaseModel):
    conversation: ConversationRead
    messages: list[MessageRead]

    @property
    def summary(self) -> str:  # pragma: no cover - convenience only
        return f"{self.conversation.title} ({len(self.messages)} messages)"
