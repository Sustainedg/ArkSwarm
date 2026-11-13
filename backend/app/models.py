"""数据库结构。

产品的核心概念：

* :class:`Gateway`      —— 网关：一个 OpenAI / Anthropic / Gemini 兼容的端点。
  分「自定义网关」与「官方链路」两类，**是 API 密钥的唯一来源**。
* :class:`Credential`   —— 凭据：工具/服务要用的第三方密钥（全局一份，可被多个智能体引用）。
* :class:`AgentProfile` —— 智能体：人格 + 内核 + 网关 + 默认模型。
  用户配置与使用的最小单位（原「角色 + 智能体实例」合并而来）。
* :class:`Conversation` —— 对话，绑定某个智能体并记住内核侧的会话 ID。
* :class:`Skill`        —— 从各智能体内核扫描出来的技能（统一编目）。
* :class:`AgentSkill`   —— 智能体与技能的分配关系（智能体能用的技能只能来自这里）。
* :class:`McpServer`    —— 统一编目的 MCP 服务器（平台新建的「通用」服务器 + 从各内核配置扫出来的）。
* :class:`AgentMcpServer` —— 智能体与 MCP 服务器的分配关系（智能体能挂的 MCP 只能来自这里）。
* :class:`RuntimeStatus` —— 各运行时的可用性检测结果（框架层缓存，不是领域实体）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, JSON, String, Text
from sqlmodel import Field, SQLModel


#: **后来新增的可选列**：SQLite 的 ``create_all`` 只建新表、不会给已存在的表补列，
#: 所以在这里登记一次，由 ``db.init_db`` 幂等补齐（见 ``db._add_missing_columns``）。
#: 只允许追加「带默认值、旧行可以留空」的列。
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    # 「默认智能体」这一列是后加的：老行一律按「不是默认」补上，由 bootstrap 在启动时补一颗星
    "agent_profiles": {"is_default": "BOOLEAN NOT NULL DEFAULT 0"},
    "gateways": {},
    # 「技能市场页地址」这一列是后加的：老行一律按空串补上（来源仍复用已有的 source_kind）
    "skills": {"source_url": "VARCHAR(500) NOT NULL DEFAULT ''"},
    # 「这条对话单独指定的模型」是后加的；为空表示跟随智能体的默认模型。
    # 老行留空即可，不需要回填。
    # 「最后已读时间」是后加的；为空表示升级前的旧对话，一律当已读（否则升级后满屏未读）。
    "conversations": {"model": "VARCHAR(160)", "last_read_at": "DATETIME"},
}


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Gateway(SQLModel, table=True):
    """一个模型服务端点（自定义网关或内核官方链路）。

    密钥只存在这里：智能体不再持有密钥，运行时从网关解密后注入 ``AgentContext``。
    ``kind == "official"`` 的网关由启动播种按适配器声明生成（``type_id`` 记录它属于哪个内核）；
    ``kind == "custom"`` 的网关由用户在设置页创建，``type_id`` 为空。
    """

    __tablename__ = "gateways"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    #: 取值：official（内核官方链路）/ custom（用户自建）
    kind: str = Field(default="custom", sa_column=Column(String(16), nullable=False))
    #: official 网关绑定的内核 id；custom 网关为空
    type_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    #: 取值：openai / anthropic / gemini —— 决定探测模型时的请求形状
    protocol: str = Field(default="openai", sa_column=Column(String(32), nullable=False))
    #: 取值：bearer / x-api-key / query / none
    auth_style: str = Field(default="bearer", sa_column=Column(String(32), nullable=False))
    base_url: str = Field(sa_column=Column(String(500), nullable=False))
    api_key_encrypted: str | None = Field(default=None, sa_column=Column(Text))
    #: 用户勾选的模型白名单：智能体只能从这里选模型
    selected_models: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    #: 最近一次「获取模型」的完整结果（重新探测不会冲掉白名单）
    model_catalog: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    extra_headers: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    last_probed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    #: 软删除时间戳：仍被未删除的智能体引用时只打标记；历史对话照旧可解析到它。
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))


class AgentProfile(SQLModel, table=True):
    """一个配置好的智能体：人格 + 内核 + 网关 + 默认模型 + 适配器选项 + 凭据。"""

    __tablename__ = "agent_profiles"

    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(sa_column=Column(String(120), nullable=False))
    avatar: str = Field(default="🛠️", sa_column=Column(String(16), nullable=False))
    #: 一句话简介（列表卡片用）
    tagline: str | None = Field(default=None, sa_column=Column(String(200)))
    #: 备注
    description: str | None = Field(default=None, sa_column=Column(Text))
    system_prompt: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: 适配器 id（claude-code / codex / gemini-cli / openai-harness / custom-cli …）
    type_id: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    #: 使用的自定义网关；为空（或指向官方链路）表示「官方默认」——平台不接管路由，
    #: 运行时回落到适配器读到的本机配置（见 ``runtime/gateway.py:bind_gateway``）
    gateway_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), ForeignKey("gateways.id", ondelete="RESTRICT"), index=True),
    )
    model: str | None = Field(default=None, sa_column=Column(String(160)))
    #: 适配器声明的可配置项（键值对，形状由 ConfigField 决定）
    options: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    enabled: bool = Field(default=True)
    #: 默认智能体：新建对话不再先问「选谁」，直接用这颗星标着的那个（全局最多一个）。
    #: 老库由 `bootstrap.ensure_default_agent` 在启动时补一次；换星只走 `api/agents`。
    is_default: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    #: 软删除时间戳：删除智能体时只打标记（历史对话仍引用它）。打了标记的智能体从所有
    #: 可选列表里消失，但历史对话仍能显示当时的名字与头像、也仍可继续。
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
    #: 来源是「技能市场」时，市场技能页地址；其余来源为空
    source_url: str = Field(default="", sa_column=Column(String(500), nullable=False))
    discovered_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True))
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))


class AgentSkill(SQLModel, table=True):
    """智能体 → 技能的分配关系。智能体实际可用的技能只能来自这张表。"""

    __tablename__ = "agent_skills"

    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="CASCADE"), primary_key=True
        )
    )
    skill_id: str = Field(
        sa_column=Column(
            String(200), ForeignKey("skills.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    position: int = Field(default=0)


class McpServer(SQLModel, table=True):
    """统一编目的一个 MCP 服务器。

    两类来源：

    * ``type_id is None``（``source_kind == "manual"``）—— 用户在「MCP」页新建的**平台通用**
      服务器，可以分配给任意内核的智能体；
    * ``type_id`` 非空 —— 从某个内核自己的配置里只读扫描出来的（来源是那个内核），
      只能分配给同内核的智能体，且不可在平台上编辑 / 删除。

    ``env`` / ``headers`` 的值可能含密钥，只经 :mod:`app.security` 加密保存
    （明文键有且只有一层：接口永远只回键名与掩码）。
    """

    __tablename__ = "mcp_servers"

    id: str = Field(sa_column=Column(String(200), primary_key=True))
    name: str = Field(sa_column=Column(String(160), nullable=False))
    #: 稳定标识：写进内核配置的键名（扫描来的就是配置里的原始键名）
    slug: str = Field(sa_column=Column(String(120), nullable=False))
    description: str = Field(default="", sa_column=Column(Text, nullable=False))
    #: 来源内核 id；None 表示平台通用（可分配给任意内核）
    type_id: str | None = Field(default=None, sa_column=Column(String(64), index=True))
    #: 取值：manual（平台新建）/ user（内核配置扫描）
    source_kind: str = Field(default="manual", sa_column=Column(String(32), nullable=False))
    #: 取值：stdio / http / sse
    transport: str = Field(default="stdio", sa_column=Column(String(16), nullable=False))
    command: str | None = Field(default=None, sa_column=Column(String(500)))
    args: list[str] = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False, default=list)
    )
    url: str | None = Field(default=None, sa_column=Column(String(1000)))
    #: 「环境变量名 → 密文」。键明文、值加密；运行时解密后按内核形状写出
    env_encrypted: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    #: 「请求头名 → 密文」，同 env
    headers_encrypted: dict[str, str] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False, default=dict)
    )
    enabled: bool = Field(default=True)
    #: 扫描来的服务器在配置里是否仍存在；平台新建的恒为 True
    available: bool = Field(default=True)
    note: str | None = Field(default=None, sa_column=Column(String(400)))
    discovered_at: datetime = Field(
        default_factory=utcnow, sa_column=Column(DateTime(timezone=True))
    )
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    #: 软删除时间戳：仅平台新建的服务器可删；历史对话与协作历史照旧可读
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))


class AgentMcpServer(SQLModel, table=True):
    """智能体 → MCP 服务器的分配关系。智能体实际能挂的 MCP 只能来自这张表。"""

    __tablename__ = "agent_mcp_servers"

    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="CASCADE"), primary_key=True
        )
    )
    mcp_server_id: str = Field(
        sa_column=Column(
            String(200), ForeignKey("mcp_servers.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    position: int = Field(default=0)


class Credential(SQLModel, table=True):
    """一条凭据：智能体干活要用的第三方密钥（GitHub token、数据库口令…）。

    与 :class:`Gateway` 的密钥不是一回事：网关密钥属于「模型提供方」，凭据属于
    「工具 / 服务」，是**全局一份**、可被多个智能体引用（``agent_credentials``）。
    值只经 :mod:`app.security` 加密保存，运行时解密后按环境变量注入（见 ``app.credentials``），
    接口永远只回掩码。
    """

    __tablename__ = "credentials"

    id: str = Field(default_factory=new_id, primary_key=True)
    #: 同时就是要注入的环境变量名（``^[A-Za-z_][A-Za-z0-9_]{0,63}$``）
    name: str = Field(sa_column=Column(String(64), nullable=False, index=True))
    #: 用途说明（给人和模型看的「名字清单」用）
    description: str | None = Field(default=None, sa_column=Column(String(200)))
    value_encrypted: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    #: 软删除：仍被未删除的智能体引用时只打标记（历史运行照旧可解析）
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))


class AgentCredential(SQLModel, table=True):
    """智能体 → 凭据的引用关系。智能体运行时能拿到的凭据只能来自这张表。"""

    __tablename__ = "agent_credentials"

    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="CASCADE"), primary_key=True
        )
    )
    credential_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("credentials.id", ondelete="RESTRICT"), primary_key=True
        )
    )
    position: int = Field(default=0)


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: str = Field(default_factory=new_id, primary_key=True)
    title: str = Field(default="新对话", sa_column=Column(String(200), nullable=False))
    #: 创建时对智能体做的快照；历史对话必须可复现
    agent_profile_id: str = Field(
        sa_column=Column(
            String(64), ForeignKey("agent_profiles.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    session_id: str | None = Field(default=None, sa_column=Column(String(160)))
    workspace_dir: str | None = Field(default=None, sa_column=Column(String(500)))
    #: 这条对话单独指定的模型；为空表示跟随智能体的默认模型（换模型不影响会话记忆）
    model: str | None = Field(default=None, sa_column=Column(String(160)))
    archived: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
    #: 用户最后一次看这条对话的时间；列表据此判断「AI 回复完了但还没读」。
    #: 为空表示升级前的旧对话 —— 一律当已读，避免升级后所有历史对话都亮未读。
    last_read_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))


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


class PlatformSetting(SQLModel, table=True):
    """平台级设置的键值存储（代理地址等）。

    与网关 / 凭据不同：这里存的是平台自身的行为参数，不绑定某个智能体。
    代理地址要注入子进程环境变量，必须以明文取出，因此不加密——
    代理地址不是 API 密钥，泄露风险低。
    """

    __tablename__ = "platform_settings"

    key: str = Field(sa_column=Column(String(64), primary_key=True))
    value: str | None = Field(default=None, sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=utcnow, sa_column=Column(DateTime(timezone=True)))
