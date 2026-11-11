"""协作域的数据表。

刻意与对话域（``Conversation`` / ``Message``）分开：协作里的「一次发言」是**工单交付物**，
不是聊天记录 —— 生命周期、状态机、查询方式都不同。两者只共享 `roles` / `agent_instances`
这两个既有的领域对象。

外键一律用字符串表名声明，因此本模块不 import ``app.models`` 的实体类，
避免与 ``db.init_db`` 的导入顺序互相纠缠。

注册方式：``db.init_db`` 会 import 本模块，``SQLModel.metadata.create_all`` 才能看到这 4 张表
（有架构守卫测试盯着，见 ``tests/test_collab_architecture.py``）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text
from sqlmodel import Field, SQLModel

from ..models import new_id, utcnow


#: 协作域**后来新增的可选列**：SQLite 的 ``create_all`` 只建新表、**不会给已存在的表补列**，
#: 而本地库（``backend/data/magenta.db``）含真实数据，禁止删库重建 —— 所以在这里登记一次，
#: 由 ``db.init_db`` 幂等补齐（见 ``db._add_missing_columns``）。
#: 只允许追加「带默认值、旧行可以留空」的列；加新列时必须同时登记在这里，否则旧库启动后会报
#: "no such column"。
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "collab_tickets": {
        "owner_key": "VARCHAR(64) NOT NULL DEFAULT ''",
        "holder_key": "VARCHAR(64) NOT NULL DEFAULT ''",
        "submitter_key": "VARCHAR(64) NOT NULL DEFAULT ''",
    },
    "collab_steps": {
        "gist": "VARCHAR(300) NOT NULL DEFAULT ''",
        # 这一步把活派给了哪个节点（delegate 用）；用来在岗位会话里还原「谁把活交给了谁」
        "target_key": "VARCHAR(64) NOT NULL DEFAULT ''",
    },
}


def empty_graph() -> dict[str, Any]:
    """空编排图；作为列默认值必须是可调用的，避免所有行共享同一个 dict。"""
    return {"nodes": [], "edges": []}


class CollabTeam(SQLModel, table=True):
    """协作组：一张可视化编排图（节点 = 角色在协作中的岗位，边 = 允许的流转）。"""

    __tablename__ = "collab_teams"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    description: str | None = Field(default=None, sa_column=Column(Text))
    #: 唯一能与用户对话的节点 key（必须存在且 duty=coordinator）
    entry_key: str | None = Field(default=None, sa_column=Column(String(64)))
    #: 团队共享工作目录；为空时按运行 ID 在数据目录下分配
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    graph: dict[str, Any] = Field(
        default_factory=empty_graph, sa_column=Column(JSON, nullable=False, default=empty_graph)
    )
    budget: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))


class CollabRun(SQLModel, table=True):
    """一次协作运行（左侧列表里的一项，类比「对话」）。"""

    __tablename__ = "collab_runs"

    id: str = Field(default_factory=new_id, primary_key=True)
    team_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("collab_teams.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    title: str = Field(default="新协作", sa_column=Column(String(200), nullable=False))
    #: 用户下达的原始目标（入口角色的第一份输入）
    goal: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: running / waiting_user / done / error / cancelled / interrupted
    status: str = Field(default="running", sa_column=Column(String(24), nullable=False, index=True))
    entry_key: str | None = Field(default=None, sa_column=Column(String(64)))
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    budget: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    usage: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    steps_used: int = Field(default=0)
    tickets_used: int = Field(default=0)
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))


class CollabTicket(SQLModel, table=True):
    """工单：一次可交接的工作单元（可以有父工单，形成拆解树）。"""

    __tablename__ = "collab_tickets"

    id: str = Field(default_factory=new_id, primary_key=True)
    run_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("collab_runs.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    parent_ticket_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(64), ForeignKey("collab_tickets.id", ondelete="SET NULL"), index=True
        ),
    )
    #: 发起者角色；由用户发起时为空
    from_role_id: str | None = Field(
        default=None, sa_column=Column(String(64), ForeignKey("roles.id", ondelete="RESTRICT"))
    )
    #: 当前承担者角色
    to_role_id: str = Field(
        sa_column=Column(String(64), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False, index=True)
    )
    #: 发起/汇总该工单的节点 key（子工单完成后回到这里）
    owner_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    #: 承担者的节点 key（节点可能改绑角色，这里保留当时的岗位）
    holder_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    #: 最近一次「提交待审」的节点 key（审查者打回时默认退给它）
    submitter_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    title: str = Field(sa_column=Column(String(240), nullable=False))
    #: 工单字段（goal/requirements/acceptance/constraints/deliverables）
    brief: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: open / working / waiting_children / review / rework / done / rejected / blocked
    state: str = Field(default="open", sa_column=Column(String(24), nullable=False, index=True))
    #: 已返工轮次
    round: int = Field(default=0)
    max_rounds: int = Field(default=0)
    verdict: str | None = Field(default=None, sa_column=Column(String(24)))
    review_comment: str | None = Field(default=None, sa_column=Column(Text))
    #: 最近一次交付摘要（给下游看的一小段文字 + 产物清单）
    result: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    closed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))


class CollabStep(SQLModel, table=True):
    """协作时间线上的一个条目。

    大多数条目是「某个角色在一个工单上的一轮发言」；用户自己说的话也存在这里
    （``role_key="user"``、``role_id``/``agent_id`` 为空），这样一次运行的时间线
    只有一张表、天然有序。结构刻意与 ``messages`` 表同构（``content`` + ``parts``），
    前端可以直接用 ``MessageView`` 渲染。
    """

    __tablename__ = "collab_steps"

    id: str = Field(default_factory=new_id, primary_key=True)
    run_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("collab_runs.id", ondelete="CASCADE"), nullable=False, index=True
        )
    )
    ticket_id: str | None = Field(
        default=None,
        sa_column=Column(
            String(64), ForeignKey("collab_tickets.id", ondelete="SET NULL"), index=True
        ),
    )
    #: 用户自己的发言没有角色与智能体
    role_id: str | None = Field(
        default=None, sa_column=Column(String(64), ForeignKey("roles.id", ondelete="RESTRICT"))
    )
    agent_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), ForeignKey("agent_instances.id", ondelete="RESTRICT")),
    )
    role_key: str = Field(default="", sa_column=Column(String(64), nullable=False))
    duty: str = Field(default="", sa_column=Column(String(24), nullable=False))
    sequence: int = Field(default=0)
    #: 解析出的动作；解析失败时存空串，并由 error 字段说明原因
    action: str = Field(default="", sa_column=Column(String(24), nullable=False))
    #: delegate 时的目标节点 key（即这一步把活交给了谁）；其他动作为空串
    target_key: str = Field(default="", sa_column=Column(String(64), nullable=False, default=""))
    #: streaming / complete / error / cancelled
    status: str = Field(default="streaming", sa_column=Column(String(16), nullable=False, index=True))
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: 由引擎从工单（结构化字段）派生出的一句话摘要 —— 交给下游时只看它，不看 content
    gist: str = Field(default="", sa_column=Column(String(300), nullable=False))
    parts: list[dict[str, Any]] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    usage: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    error: str | None = Field(default=None, sa_column=Column(Text))
    #: 越权审计结果（只读节点运行前后共享目录的指纹对比）
    audit: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    started_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
