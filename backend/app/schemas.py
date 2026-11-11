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


class RuntimeAction(BaseModel):
    """卸载请求体：破坏性操作必须显式确认。

    只有 ``/uninstall`` 读这个模型；安装与更新不改动已有配置，不需要确认，也不声明请求体。
    """

    confirm: bool = False


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


class RoleSkillBrief(BaseModel):
    """角色卡片上展示的技能摘要。"""

    id: str
    name: str
    type_id: str
    source_kind: str
    available: bool = True


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
    #: 该角色分配到的技能（技能只能通过分配获得）
    skill_ids: list[str] = Field(default_factory=list)
    skills: list[RoleSkillBrief] = Field(default_factory=list)


class RoleSkillsUpdate(BaseModel):
    """整表替换角色的技能分配。"""

    skill_ids: list[str] = Field(default_factory=list)


class SkillRead(BaseModel):
    """技能列表项。"""

    id: str
    type_id: str
    type_name: str | None = None
    slug: str
    name: str
    description: str = ""
    source_kind: str
    source_label: str = ""
    version: str | None = None
    author: str | None = None
    when_to_use: str | None = None
    available: bool = True
    total_bytes: int = 0
    file_count: int = 0
    note: str | None = None
    used_by_roles: int = 0


class SkillDetail(SkillRead):
    """技能详情：完整描述、元数据、文件清单，以及引用了它的角色。"""

    path: str = ""
    fingerprint: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    body: str = ""
    body_truncated: bool = False
    roles: list[dict[str, str]] = Field(default_factory=list)


class SkillRescanResult(BaseModel):
    added: int = 0
    updated: int = 0
    missing: int = 0
    total: int = 0


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


