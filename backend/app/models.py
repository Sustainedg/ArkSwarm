"""数据库结构。

产品的核心概念：

* :class:`AgentInstance` —— 一个**配置好的**智能体（类型 + 网关 + 密钥 + 模型）。
* :class:`Role`          —— 角色，从若干智能体实例中挑一个绑定。
* :class:`Conversation`  —— 对话，绑定某个角色并记住智能体侧的会话 ID。
* :class:`Skill`         —— 从各智能体内核扫描出来的技能（统一编目）。
* :class:`RoleSkill`     —— 角色与技能的分配关系（角色能用的技能只能来自这里）。
* :class:`RuntimeStatus`  —— 各运行时的可用性检测结果（框架层缓存，不是领域实体）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, JSON, String, Text
from sqlmodel import Field, SQLModel


#: 对话域**后来新增的可选列**：SQLite 的 ``create_all`` 只建新表、不会给已存在的表补列，
#: 而本地库（``backend/data/magenta.db``）含真实数据、禁止删库重建 ——
#: 所以在这里登记一次，由 ``db.init_db`` 幂等补齐（见 ``db._add_missing_columns``）。
#: 只允许追加「带默认值、旧行可以留空」的列。
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "agent_instances": {"deleted_at": "DATETIME"},
    "roles": {"deleted_at": "DATETIME"},
}


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
    #: 软删除时间戳：被历史引用（外键 RESTRICT）时只打标记、不真删行；
    #: 规则见 ``Role.deleted_at``。
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))


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
    #: 软删除时间戳：删除角色时只打标记（历史对话/协作工单仍引用它，
    #: 硬删会打断历史）。打了标记的角色从所有可选列表里消失，
    #: 但历史记录仍能显示当时的角色名与头像。
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))



class Skill(SQLModel, table=True):
    """从各智能体内核的技能根扫描出来的技能。

    ``id`` 是稳定标识 ``<type_id>:<slug>``：技能目录暂时消失时只把 ``available``
    置为 False，不删行，这样历史分配关系不会断。
    """

    __tablename__ = "skills"

    id: str = Field(sa_column=Column(String(200), primary_key=True))
    type_id: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    slug: str = Field(sa_column=Column(String(120), nullable=False))
    name: str = Field(sa_column=Column(String(160), nullable=False))
    description: str = Field(default="", sa_column=Column(Text, nullable=False))
    source_kind: str = Field(default="user", sa_column=Column(String(32), nullable=False))
    path: str = Field(default="", sa_column=Column(String(1000), nullable=False))
    fingerprint: str = Field(default="", sa_column=Column(String(64), nullable=False))
    version: str | None = Field(default=None, sa_column=Column(String(80)))
    author: str | None = Field(default=None, sa_column=Column(String(160)))
    when_to_use: str | None = Field(default=None, sa_column=Column(Text))
    allowed_tools: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    files: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    total_bytes: int = Field(default=0)
    meta: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    available: bool = Field(default=True)
    note: str | None = Field(default=None, sa_column=Column(String(400)))
    discovered_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True))
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))


class RoleSkill(SQLModel, table=True):
    """角色 → 技能的分配关系。角色实际可用的技能只能来自这张表。"""

    __tablename__ = "role_skills"

    role_id: str = Field(
        sa_column=Column(String(64), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    )
    skill_id: str = Field(
        sa_column=Column(
            String(200), ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    position: int = Field(default=0)


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(default="新对话", sa_column=Column(String(200), nullable=False))
    role_id: str = Field(sa_column=Column(String(64), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True))
    #: 创建时对智能体做的快照；历史对话必须可复现
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
    role: str = Field(sa_column=Column(String(16), nullable=False))  # 取值：user / assistant / system
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    parts: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False, default=list))
    usage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict))
    status: str = Field(default="complete", sa_column=Column(String(16), nullable=False))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True), index=True))


class RuntimeStatus(SQLModel, table=True):
    """某个运行时（CLI 内核）在本机的一次可用性检测结果。

    这是框架层的**缓存**，不是领域实体：内核装没装、是什么版本，只有跑一次
    ``内核 --version`` 才知道，而 Node 系 CLI 的冷启动接近 2 秒 —— 这种同步子进程
    放在请求路径上会冻住整个事件循环（详见 ``runtime/availability.py``）。
    因此由框架定时检测后写在这里，接口只读这张表。``type_id`` 就是适配器的 id。
    """

    __tablename__ = "runtime_status"

    type_id: str = Field(sa_column=Column(String(64), primary_key=True))
    installed: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False))
    version: str | None = Field(default=None, sa_column=Column(String(160)))
    #: 已安装时是可执行文件路径，未安装时是原因（与 ``Availability.detail`` 同义）。
    detail: str | None = Field(default=None, sa_column=Column(Text))
    #: 本次检测的时间（UTC）。超过 ``runtime.availability.CHECK_INTERVAL`` 即视为过期。
    checked_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
