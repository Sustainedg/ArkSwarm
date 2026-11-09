"""Database schema.

Three concepts drive the product:

* :class:`AgentInstance` — a *configured* agent (type + gateway + key + model).
* :class:`Role`          — a persona that picks one agent instance.
* :class:`Conversation`  — a chat bound to a role, remembering the agent session.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text
from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentInstance(SQLModel, table=True):
    __tablename__ = "agent_instances"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    type_id: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    description: str | None = Field(default=None, sa_column=Column(Text))
    gateway_url: str | None = Field(default=None, sa_column=Column(String(500)))
    model: str | None = Field(default=None, sa_column=Column(String(160)))
    api_key_encrypted: str | None = Field(default=None, sa_column=Column(Text))
    options: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))


class Role(SQLModel, table=True):
    __tablename__ = "roles"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    avatar: str = Field(default="🛠️", sa_column=Column(String(16), nullable=False))
    tagline: str | None = Field(default=None, sa_column=Column(String(200)))
    system_prompt: str = Field(default="", sa_column=Column(Text, nullable=False))
    agent_id: str = Field(sa_column=Column(String(64), ForeignKey("agent_instances.id", ondelete="RESTRICT"), nullable=False, index=True))
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    parameters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))



class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(default="New conversation", sa_column=Column(String(200), nullable=False))
    role_id: str = Field(sa_column=Column(String(64), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True))
    #: snapshot of the agent used at creation time; history must stay reproducible
    agent_id: str = Field(sa_column=Column(String(64), ForeignKey("agent_instances.id", ondelete="RESTRICT"), nullable=False, index=True))
    session_id: str | None = Field(default=None, sa_column=Column(String(160)))
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    archived: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))



class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=new_id, primary_key=True)
    conversation_id: str = Field(sa_column=Column(String(64), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True))
    role: str = Field(sa_column=Column(String(16), nullable=False))  # user | assistant | system
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    parts: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    status: str = Field(default="complete", sa_column=Column(String(16), nullable=False))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), index=True))

