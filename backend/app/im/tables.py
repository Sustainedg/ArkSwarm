"""IM 的数据表：连接、配对账号、聊天绑定。

IM 是**通道**，不是成员：一条连接把「聊天软件里的一个机器人」绑到一个智能体上，
聊天软件里的用户消息直接进那个智能体当前的那段对话，回复再发回来源聊天。
因此这里只有三张表，都不与 ``Conversation`` 合并：

* :class:`ImConnection` —— 一条连接（平台 + 账号 + 凭据 + 绑定的智能体 + 状态）；
* :class:`ImPeer` —— 配对过的账号（没配对的账号发消息只回一句「已发出连接请求」）；
* :class:`ImChat` —— 见过的一个聊天（私聊 / 群），群里第一次 @ 机器人时出现，
  由用户在设置里指定它绑到哪个智能体。

凭据值只经 :mod:`app.security` 加密保存，接口永远只回「哪些字段已填」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text
from sqlmodel import Field, SQLModel

from ..models import new_id, utcnow

#: **后来新增的可选列**：SQLite 的 ``create_all`` 只建新表、不会给已存在的表补列，
#: 所以在这里登记一次，由 ``db.init_db`` 幂等补齐（见 ``db._add_missing_columns``）。
#: 只允许追加「带默认值、旧行可以留空」的列。
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "im_connections": {
        "last_error": "TEXT",
        "last_status": "VARCHAR(24) NOT NULL DEFAULT ''",
        "runtime": "JSON NOT NULL DEFAULT '{}'",
        # 过程内容开关（思考 / 工具 / 日志）：老行一律按「都不发」补上，聊天里只有正文。
        "options": "JSON NOT NULL DEFAULT '{}'",
    },
}


class ImConnection(SQLModel, table=True):
    """一条 IM 连接：一个平台账号 + 绑定的智能体。

    ``fields`` 存非密钥字段（App ID、域名…），``secrets`` 存「字段名 → 密文」。
    两者都按 ``base.PLATFORMS`` 的声明来读写，接口永远只回字段名与掩码。
    """

    __tablename__ = "im_connections"

    id: str = Field(default_factory=new_id, primary_key=True)
    #: 平台 id（telegram / feishu / dingtalk / wecom / qq / discord / slack）
    platform: str = Field(sa_column=Column(String(32), nullable=False, index=True))
    #: 平台侧账号名（连接成功后由 ``check`` 给出，仅用于展示）
    account_name: str = Field(default="", sa_column=Column(String(160), nullable=False))
    account_id: str = Field(default="", sa_column=Column(String(160), nullable=False))
    #: 非密钥字段（字段名 → 值）
    fields: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: 密钥字段（字段名 → 密文）；明文只出现在运行时
    secrets: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: 绑定的智能体：所有私聊都进它；群里没单独指定时也用它
    agent_profile_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"), index=True
        ),
    )
    enabled: bool = Field(default=True, nullable=False)
    #: 最近一次运行状态：ready / connecting / retrying / error / off
    last_status: str = Field(default="off", sa_column=Column(String(24), nullable=False))
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    #: 适配器自己的小状态（长轮询游标等），重启后恢复
    runtime: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: 这个连接发什么：思考过程 / 工具调用 / 日志（默认都不发，聊天里只有正文）
    options: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    #: 所属用户 id；老库由 bootstrap 回填给首任管理员。
    owner_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))


class ImPeer(SQLModel, table=True):
    """配对过的聊天软件账号：只有它们说的话会进智能体。"""

    __tablename__ = "im_peers"

    id: str = Field(default_factory=new_id, primary_key=True)
    connection_id: str = Field(
        sa_column=Column(
            String(64),
            ForeignKey("im_connections.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    #: 平台侧的发送者 id
    sender_id: str = Field(sa_column=Column(String(160), nullable=False, index=True))
    sender_name: str = Field(default="", sa_column=Column(String(160), nullable=False))
    #: pending（等用户允许）/ allowed（已配对）/ denied（用户拒绝过）
    state: str = Field(default="pending", sa_column=Column(String(16), nullable=False, index=True))
    #: 第一次发起请求时所在的聊天（允许后顺手建出这条聊天）
    chat_key: str = Field(default="", sa_column=Column(String(200), nullable=False))
    chat_kind: str = Field(default="direct", sa_column=Column(String(16), nullable=False))
    chat_title: str = Field(default="", sa_column=Column(String(200), nullable=False))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))


class ImChat(SQLModel, table=True):
    """连接见过的一个聊天；群里由用户指定绑到哪个智能体。"""

    __tablename__ = "im_chats"

    id: str = Field(default_factory=new_id, primary_key=True)
    connection_id: str = Field(
        sa_column=Column(
            String(64),
            ForeignKey("im_connections.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    chat_key: str = Field(sa_column=Column(String(200), nullable=False, index=True))
    kind: str = Field(default="direct", sa_column=Column(String(16), nullable=False))
    title: str = Field(default="", sa_column=Column(String(200), nullable=False))
    #: 群里单独绑定的智能体；为空表示跟随连接的绑定
    agent_profile_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT")),
    )
    #: 这个聊天对应的那条对话（每条聊天一条，保持上下文连续）
    conversation_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(64), ForeignKey("conversations.id", ondelete="SET NULL")
        ),
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))


__all__ = ["ADDITIVE_COLUMNS", "ImChat", "ImConnection", "ImPeer"]
