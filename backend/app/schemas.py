"""HTTP 接口的请求体与响应体模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --- 网关 -------------------------------------------------------------------


class GatewayCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    #: openai / anthropic / gemini
    protocol: str = "openai"
    #: bearer / x-api-key / query / none
    auth_style: str = "bearer"
    api_key: str | None = None
    extra_headers: dict[str, Any] = Field(default_factory=dict)
    #: 模型白名单，必须至少有一个（这就是「必须成功才能保存」的服务端不变量）
    selected_models: list[str] = Field(min_length=1)
    enabled: bool = True


class GatewayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    protocol: str | None = None
    auth_style: str | None = None
    #: 省略表示保留已存密钥，传 "" 表示清除
    api_key: str | None = None
    extra_headers: dict[str, Any] | None = None
    selected_models: list[str] | None = Field(default=None, min_length=1)
    enabled: bool | None = None


class GatewayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: str
    type_id: str | None = None
    protocol: str
    auth_style: str
    base_url: str
    selected_models: list[str] = Field(default_factory=list)
    model_catalog: list[str] = Field(default_factory=list)
    extra_headers: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_probed_at: datetime | None = None
    #: 派生字段
    type_name: str | None = None
    api_key_set: bool = False
    api_key_preview: str | None = None
    #: 有多少个未删除的智能体在用这个网关
    used_by_profiles: int = 0


class GatewayProbeRequest(BaseModel):
    """「获取模型」按钮的请求体：不落库，只探测一次。"""

    base_url: str = Field(min_length=1, max_length=500)
    protocol: str = "openai"
    auth_style: str = "bearer"
    api_key: str | None = None
    extra_headers: dict[str, Any] = Field(default_factory=dict)
    #: 重新探测已存网关时带上 id：密钥留空即复用已存密钥
    gateway_id: str | None = None


class GatewayProbeResult(BaseModel):
    ok: bool
    message: str
    models: list[str] = Field(default_factory=list)


# --- 智能体 -----------------------------------------------------------------


class CredentialCreate(BaseModel):
    """在凭据库里新建一条凭据。"""

    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    value: str = Field(min_length=1)


class CredentialUpdate(BaseModel):
    """改名 / 改说明 / 换值；没给的字段一律不改（``value`` 留空＝不换值）。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    value: str | None = None


class CredentialRead(BaseModel):
    """凭据的只读视图：只有名字、说明与掩码，永远不回传明文。"""

    id: str
    name: str
    description: str | None = None
    has_value: bool = False
    masked: str | None = None
    used_by_agents: int = 0
    created_at: datetime
    updated_at: datetime


class AgentCredentialInput(BaseModel):
    """智能体配置里提交的一条凭据引用。

    ``id`` 有值＝引用凭据库里已有的那条；没有 ``id`` 时按 ``name`` 找同名凭据，
    再找不到就用 ``value`` 新建一条（于是「直接挑」和「顺手新建」都是一次保存搞定）。
    """

    id: str | None = None
    name: str = Field(min_length=1, max_length=64)
    description: str | None = None
    value: str | None = None


class AgentCredentialBrief(BaseModel):
    """智能体卡片 / 弹窗上展示的凭据摘要。"""

    id: str
    name: str
    description: str | None = None
    has_value: bool = False
    masked: str | None = None


class AgentProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type_id: str
    avatar: str = "🛠️"
    tagline: str | None = None
    description: str | None = None
    system_prompt: str = ""
    gateway_id: str | None = None
    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    credentials: list[AgentCredentialInput] | None = None
    enabled: bool = True
    #: 创建时就设为默认智能体（第一个智能体不传也会自动成为默认）
    is_default: bool = False
    #: 分组名（智能体页按它折叠分组；空串 = 未分组）
    group_name: str = Field(default="", max_length=60)


class AgentProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    #: 设为 / 取消「默认智能体」（智能体卡片右上角那颗五角星）
    is_default: bool | None = None
    type_id: str | None = None
    avatar: str | None = None
    tagline: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    gateway_id: str | None = None
    model: str | None = None
    options: dict[str, Any] | None = None
    credentials: list[AgentCredentialInput] | None = None
    enabled: bool | None = None
    #: 分组名；空串表示移出分组（归入「未分组」）
    group_name: str | None = Field(default=None, max_length=60)


class AgentSkillBrief(BaseModel):
    """智能体卡片上展示的技能摘要。"""

    id: str
    name: str
    type_id: str
    source_kind: str
    available: bool = True


class AgentMcpBrief(BaseModel):
    """智能体卡片上展示的 MCP 服务器摘要（永远没有密钥明文）。"""

    id: str
    name: str
    slug: str
    transport: str
    type_id: str | None = None
    available: bool = True
    enabled: bool = True


class AgentProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    avatar: str
    tagline: str | None = None
    description: str | None = None
    system_prompt: str
    type_id: str
    gateway_id: str | None = None
    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    #: 默认智能体：新建对话时不问就用它（全局唯一）
    is_default: bool = False
    #: 分组名（空串 = 未分组）
    group_name: str = ""
    created_at: datetime
    updated_at: datetime
    #: 派生字段
    type_name: str | None = None
    runtime: str | None = None
    availability: dict[str, Any] | None = None
    gateway_name: str | None = None
    gateway_base_url: str | None = None
    gateway_kind: str | None = None
    gateway_api_key_set: bool = False
    credentials: list[AgentCredentialBrief] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    skills: list[AgentSkillBrief] = Field(default_factory=list)
    mcp_ids: list[str] = Field(default_factory=list)
    mcp_servers: list[AgentMcpBrief] = Field(default_factory=list)


class AgentTestResult(BaseModel):
    ok: bool
    message: str
    checks: list[dict[str, Any]] = Field(default_factory=list)


class AgentSkillsUpdate(BaseModel):
    """整表替换智能体的技能分配。"""

    skill_ids: list[str] = Field(default_factory=list)


class AgentMcpUpdate(BaseModel):
    """整表替换智能体的 MCP 分配。"""

    mcp_ids: list[str] = Field(default_factory=list)


class RuntimeAction(BaseModel):
    """卸载请求体：破坏性操作必须显式确认。

    只有 ``/uninstall`` 读这个模型；安装与更新不改动已有配置，不需要确认，也不声明请求体。
    """

    confirm: bool = False


# --- 技能 -------------------------------------------------------------------


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
    used_by_agents: int = 0
    #: 由「技能市场」装入的技能为 True（可在市场页卸载），其余只读
    is_market: bool = False
    #: 能否从技能页删除：内核自带（system）之外的技能都可以
    deletable: bool = False
    #: 市场技能对应的市场页地址
    source_url: str | None = None


class SkillDetail(SkillRead):
    """技能详情：完整描述、元数据、文件清单，以及引用了它的智能体。"""

    path: str = ""
    fingerprint: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    body: str = ""
    body_truncated: bool = False
    agents: list[dict[str, str]] = Field(default_factory=list)


class SkillRescanResult(BaseModel):
    added: int = 0
    updated: int = 0
    missing: int = 0
    total: int = 0


# --- 技能市场（SkillHub）-----------------------------------------------------


class SkillHubInstallIn(BaseModel):
    """把一个市场技能安装到某个内核的原生技能根。"""

    #: 市场里的技能标识（slug）
    slug: str
    #: 目标内核 id（如 claude-code / codex）；装到该内核声明的技能目录里
    type_id: str
    #: 指定版本；留空装最新版
    version: str | None = None


class SkillHubUninstallIn(BaseModel):
    """卸载一个由市场装入的技能（``skill_id`` 形如 ``claude-code:foo``）。"""

    skill_id: str


# --- MCP 服务器 -------------------------------------------------------------


class McpServerCreate(BaseModel):
    """新建一个「平台通用」的 MCP 服务器（可分配给任意内核的智能体）。"""

    name: str
    slug: str | None = None
    description: str | None = None
    #: stdio / http / sse
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    #: 启动命令的环境变量 / 请求头；值会加密保存，接口只回掩码
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class McpServerUpdate(BaseModel):
    """更新一个平台通用的 MCP 服务器；留空的值表示不改（界面回显的是掩码）。"""

    name: str | None = None
    slug: str | None = None
    description: str | None = None
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None


class McpBuiltinParamRead(BaseModel):
    """内置 MCP 的一个业务参数（平台地址、令牌、网关地址、模型名…）；值只回掩码。"""

    key: str
    label: str
    help: str = ""
    placeholder: str = ""
    required: bool = False
    secret: bool = False
    default: str = ""
    masked: str | None = None
    #: 由平台自动填写 / 刷新（系统管理 MCP 的平台地址与令牌），不需要用户输入
    managed: bool = False


class McpServerRead(BaseModel):
    """MCP 目录项：只回键名与掩码，永远没有明文密钥。"""

    id: str
    name: str
    slug: str
    description: str = ""
    #: 来源内核 id；为空表示平台通用（可分配给任意内核）
    type_id: str | None = None
    type_name: str | None = None
    source_kind: str
    source_label: str = ""
    transport: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env_keys: list[str] = Field(default_factory=list)
    env_preview: dict[str, str | None] = Field(default_factory=dict)
    header_keys: list[str] = Field(default_factory=list)
    header_preview: dict[str, str | None] = Field(default_factory=dict)
    enabled: bool = True
    available: bool = True
    note: str | None = None
    #: 平台是否接管它的内容（平台新建的为 True；扫描来的为 False）
    editable: bool = False
    used_by_agents: int = 0
    #: 内置服务器的稳定标识（例如 image-gen / arkswarm-admin）；普通服务器为空
    builtin_key: str | None = None
    #: 内置服务器要用户填写的参数说明；普通服务器为空数组
    builtin_params: list[McpBuiltinParamRead] = Field(default_factory=list)


class McpServerDetail(McpServerRead):
    """MCP 服务器详情：附上引用了它的智能体。"""

    agents: list[dict[str, str]] = Field(default_factory=list)


class McpRescanResult(BaseModel):
    added: int = 0
    updated: int = 0
    missing: int = 0
    total: int = 0


# --- 对话 -------------------------------------------------------------------


class ConversationCreate(BaseModel):
    title: str | None = None
    #: 不传就用「默认智能体」（见 `runtime.service.pick_default_profile`）
    agent_profile_id: str | None = None
    workspace_dir: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    archived: bool | None = None
    #: 对话中途换智能体 / 换工作目录（聊天页输入框上方那两个小图标）；
    #: 两者都会清空会话记忆，见 `api/conversations.update_conversation`
    agent_profile_id: str | None = None
    workspace_dir: str | None = None
    #: 这条对话单独使用的模型；传 null / 空串表示回到智能体的默认模型。
    #: 与换智能体不同，换模型不清空会话记忆。
    model: str | None = Field(default=None, max_length=160)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    agent_profile_id: str
    session_id: str | None = None
    workspace_dir: str | None = None
    #: 这条对话单独指定的模型；为空表示跟随智能体默认
    model: str | None = None
    archived: bool
    created_at: datetime
    updated_at: datetime
    agent_name: str | None = None
    agent_avatar: str | None = None
    agent_type_id: str | None = None
    message_count: int = 0
    #: 最后一条是**已完成**的助手回复、且晚于用户最后一次打开这条对话的时间
    unread: bool = False


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


class AttachmentUpload(BaseModel):
    """上传一个附件。

    内容走 base64 而不是 multipart：一次上传本来就只有几百 KB，这样不必为它引入
    `python-multipart` 依赖（见 AGENTS.md「不要新增依赖」）。
    """

    name: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1)


class SendMessage(BaseModel):
    #: 只发附件时正文可以为空
    content: str = ""
    #: 已上传附件的相对路径（相对对话工作目录，由 `/attachments` 接口返回）
    attachments: list[str] = Field(default_factory=list)
    #: 可选的单轮覆盖项
    workspace_dir: str | None = None
    reset_session: bool = False

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value

    @model_validator(mode="after")
    def _require_something(self) -> SendMessage:
        if not self.content.strip() and not self.attachments:
            raise ValueError("消息内容不能为空")
        return self
