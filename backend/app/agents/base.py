"""每个智能体集成都要实现的契约。

接入一个全新的智能体（Gemini CLI、DeepSeek 内核、Aider…），只需在
``app/agents/builtin`` 下新增一个模块，定义 :class:`AgentDefinition` 的子类
并用 :func:`register_agent` 装饰即可，代码库其他部分无需改动。
"""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, ClassVar, Iterable, Sequence

from .events import AgentEvent, EventType
from .skills import SkillMount, SkillSpec


class FieldType(str, Enum):
    STRING = "string"
    SECRET = "secret"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SELECT = "select"
    TEXTAREA = "textarea"
    KEYVALUE = "keyvalue"
    STRING_LIST = "string_list"


class Runtime(str, Enum):
    """适配器与智能体的通信方式。"""

    CLI = "cli"  # 启动子进程
    API = "api"  # 进程内直接发起 HTTP 调用（OpenAI 兼容端点）


@dataclass(slots=True)
class ConfigField:
    """一个可由用户编辑的配置项的声明式描述。

    界面会统一按声明渲染这些字段，因此新增智能体自带表单，无需改动前端。
    """

    key: str
    label: str
    type: FieldType = FieldType.STRING
    required: bool = False
    default: Any = None
    placeholder: str | None = None
    help: str | None = None
    options: list[dict[str, str]] | None = None
    advanced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type.value,
            "required": self.required,
            "default": self.default,
            "placeholder": self.placeholder,
            "help": self.help,
            "options": self.options,
            "advanced": self.advanced,
        }


@dataclass(slots=True)
class Capabilities:
    streaming: bool = True
    sessions: bool = False          # 能否在服务端续接会话 / 线程
    native_system_prompt: bool = False
    tools: bool = False
    mcp: bool = False
    permission_modes: bool = False
    working_directory: bool = False
    #: 是否支持技能（声明即表示适配器实现了 discover_skills/skill_mount）
    skills: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "streaming": self.streaming,
            "sessions": self.sessions,
            "native_system_prompt": self.native_system_prompt,
            "tools": self.tools,
            "mcp": self.mcp,
            "permission_modes": self.permission_modes,
            "working_directory": self.working_directory,
            "skills": self.skills,
        }


@dataclass(slots=True)
class Availability:
    installed: bool
    version: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"installed": self.installed, "version": self.version, "detail": self.detail}


@dataclass(slots=True)
class Invocation:
    """一次运行所需的完整启动描述（子进程或 HTTP 目标）。"""

    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    stdin: str | None = None
    #: 运行器在启动前必须写入的文件（相对路径 -> 内容）
    files: dict[str, str] = field(default_factory=dict)
    #: 适配器期望事件载荷出现在哪条流上
    stream: str = "stdout"


@dataclass(slots=True)
class AgentContext:
    """从数据库解析出的、适配器构建一次运行所需的全部上下文。"""

    agent_id: str
    agent_name: str
    #: 智能体实例上合并后的 options
    options: dict[str, Any] = field(default_factory=dict)
    gateway_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    #: 人格
    role_name: str | None = None
    system_prompt: str | None = None
    #: 工作目录
    workspace_dir: str | None = None
    #: 多轮会话支持
    session_id: str | None = None
    #: 对话记录；注入给无法在服务端续接会话的智能体
    history: list[dict[str, str]] = field(default_factory=list)
    prompt: str = ""
    #: 角色分配到的技能（框架按绑定智能体的类型过滤后注入）
    skills: list[SkillSpec] = field(default_factory=list)
    #: 框架物化好的技能主目录；适配器据此设置内核的配置目录环境变量
    skill_home_dir: str | None = None

    def option(self, key: str, default: Any = None) -> Any:
        value = self.options.get(key, default)
        return default if value in (None, "") else value


class AgentDefinition(ABC):
    """单个智能体集成的静态描述与运行行为。"""

    id: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    vendor: ClassVar[str] = ""
    runtime: ClassVar[Runtime] = Runtime.CLI
    capabilities: ClassVar[Capabilities] = Capabilities()
    config_fields: ClassVar[Sequence[ConfigField]] = ()
    #: 按「自带网关 / 自带密钥」约定使用的环境变量名
    api_key_env: ClassVar[str | None] = None
    base_url_env: ClassVar[str | None] = None
    default_gateway: ClassVar[str | None] = None
    default_model: ClassVar[str | None] = None
    docs_url: ClassVar[str | None] = None
    #: 必须存在于 PATH 中的可执行文件（仅 CLI 适配器）
    executable: ClassVar[str | None] = None
    #: 首次启动播种时是否允许为该适配器创建实例
    #: （需要用户先填写配置才能工作的模板设为 False）
    auto_provision: ClassVar[bool] = True

    # -- 元数据与自省 --------------------------------------------------------

    def descriptor(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "vendor": self.vendor,
            "runtime": self.runtime.value,
            "capabilities": self.capabilities.to_dict(),
            "config_fields": [f.to_dict() for f in self.config_fields],
            "api_key_env": self.api_key_env,
            "base_url_env": self.base_url_env,
            "default_gateway": self.default_gateway,
            "default_model": self.default_model,
            "docs_url": self.docs_url,
            "executable": self.executable,
            "availability": self.availability().to_dict(),
        }

    def availability(self) -> Availability:
        if not self.executable:
            return Availability(True, detail="进程内运行，无需本地可执行文件")
        path = shutil.which(self.executable)
        if not path:
            return Availability(False, detail=f"未在 PATH 中找到 `{self.executable}`")
        return Availability(True, version=self._probe_version(path), detail=path)

    def _probe_version(self, path: str) -> str | None:
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=8, check=False
            )
            return (out.stdout or out.stderr).strip().splitlines()[0][:80] or None
        except Exception:  # noqa: BLE001 - 可用性探测绝不能向调用方抛异常
            return None

    def system_defaults(self) -> dict[str, Any]:
        """本机真实配置里的默认值（网关地址 / 模型 / 凭据 / 选项）。

        适配器可以覆盖此钩子，从 CLI 自身的系统级配置（例如 Claude Code 的
        ``~/.claude/settings.json``）读取真实默认值。框架在「新建智能体」或
        「字段仍是内置默认值」时用它兜底，从而让界面展示与一次真实运行都对齐
        本机的真实配置。适配器只允许依赖标准库，因此在这里读文件是安全的。
        默认返回空字典。
        """
        return {}

    def resolve_gateway(self, gateway_url: str | None) -> str | None:
        """把等于内置默认值的网关视为「未自定义」，回落到本机真实配置。"""
        if gateway_url and gateway_url != self.default_gateway:
            return gateway_url
        return self.system_defaults().get("gateway_url") or gateway_url

    def resolve_model(self, model: str | None) -> str | None:
        """把等于内置默认值的模型视为「未自定义」，回落到本机真实配置。"""
        if model and model != self.default_model:
            return model
        return self.system_defaults().get("model") or model

    def fields_by_key(self) -> dict[str, ConfigField]:
        return {f.key: f for f in self.config_fields}

    def coerce_options(self, raw: dict[str, Any]) -> dict[str, Any]:
        """按 config_fields 校验并规整用户提交的 options。"""
        coerced: dict[str, Any] = {}
        for field_ in self.config_fields:
            if field_.key in raw:
                value = raw[field_.key]
                if field_.type is FieldType.NUMBER and value is not None:
                    value = float(value) if isinstance(value, str) and "." in value else int(value)
                elif field_.type is FieldType.BOOLEAN:
                    value = bool(value)
            else:
                value = field_.default
            if value not in (None, "", [], {}):
                coerced[field_.key] = value
        for key, value in (raw or {}).items():  # 保留未知键，保持前向兼容
            coerced.setdefault(key, value)
        return coerced

    def effective_env(self, ctx: AgentContext) -> dict[str, str]:
        """按「自带网关 / 自带 API 密钥」约定生成环境变量，并叠加用户自定义项。"""
        env: dict[str, str] = {}
        if self.base_url_env and ctx.gateway_url:
            env[self.base_url_env] = ctx.gateway_url
        if self.api_key_env and ctx.api_key:
            env[self.api_key_env] = ctx.api_key
        for key, value in (ctx.option("env") or {}).items():
            env[str(key)] = str(value)
        return env

    # -- 运行时 --------------------------------------------------------------

    @abstractmethod
    def build_invocation(self, ctx: AgentContext) -> Invocation:
        """把一次运行请求解析成具体的启动描述。"""

    @abstractmethod
    def parse_line(self, line: str, ctx: AgentContext) -> Iterable[AgentEvent]:
        """把一行原始输出翻译成零个或多个规范化事件。

        解析状态统一放在 ``run.state`` 这个字典里，因此适配器本身保持无状态。
        """

    def create_run(self, ctx: AgentContext) -> "AgentRun":
        return AgentRun(self, ctx)

    async def stream(self, ctx: AgentContext) -> AsyncIterator[AgentEvent]:
        """``Runtime.API`` 型适配器使用的进程内执行入口。"""
        raise NotImplementedError(f"{self.id} 未实现进程内流式接口")
        yield  # pragma: no cover - 使其成为异步生成器

    async def probe(self, ctx: AgentContext) -> tuple[bool, str]:
        """给「连通性测试」按钮用的轻量探测：检查运行环境、凭据与连通性。"""
        if self.runtime is Runtime.CLI:
            availability = self.availability()
            if not availability.installed:
                return False, availability.detail or "缺少可执行文件"
            if not ctx.api_key and not self.api_key_env:
                return True, "CLI 已就绪（该适配器无需 API 密钥）"
            if not ctx.api_key:
                return True, "CLI 已就绪（使用环境中的默认凭据）"
            return True, f"配置校验通过（{availability.version or '本地 CLI'}）"
        return await self.probe_api(ctx)

    async def probe_api(self, ctx: AgentContext) -> tuple[bool, str]:
        return True, "该适配器未提供探测实现"

    # -- 技能 ----------------------------------------------------------------

    def discover_skills(self) -> list[SkillSpec]:
        """交给平台统一编目的技能（只读扫描本机文件系统）。

        默认没有技能；据有技能目录的内核（Claude Code、Codex）覆盖此钩子。
        实现必须是尽力而为的：目录不存在、权限不足、frontmatter 损坏都不允许抛错。
        """
        return []

    def skill_mount(self, ctx: AgentContext) -> SkillMount | None:
        """声明「把角色分配到的技能物化到哪里」；返回 ``None`` 表示该内核不支持技能。

        适配器负责声明自己内核的隔离方式（配置目录、需要携带的配置文件、
        需要关闭的内核自带技能）；框架只按声明执行，不关心具体是哪个内核。
        """
        return None


class AgentRun:
    """把无状态的 :class:`AgentDefinition` 绑定到一次具体运行上。

    ``state`` 是本次运行私有的临时字典，适配器可以在里面保存解析状态
    （去重标记、工具调用记账等），无需任何全局可变结构。
    """

    def __init__(self, definition: AgentDefinition, ctx: AgentContext) -> None:
        self.definition = definition
        self.ctx = ctx
        self.state: dict[str, Any] = {}
        self.emitted_text = ""
        self.thinking = ""
        self.session_id = ctx.session_id
        self.tool_calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {}

    def build_invocation(self) -> Invocation:
        return self.definition.build_invocation(self.ctx)

    def feed(self, line: str) -> Iterable[AgentEvent]:
        for event in self.definition.parse_line(line, self):
            self._observe(event)
            yield event

    def _observe(self, event: AgentEvent) -> None:
        if event.type is EventType.MESSAGE_DELTA and event.text:
            self.emitted_text += event.text
        elif event.type is EventType.THINKING_DELTA and event.text:
            self.thinking += event.text
        elif event.type is EventType.SESSION:
            self.session_id = event.payload.get("session_id", self.session_id)
        elif event.type is EventType.TOOL_CALL:
            self.tool_calls.append({"id": event.call_id, "name": event.name, **(event.payload or {})})

    def finish(self) -> Iterable[AgentEvent]:
        """给需要在结束时补发事件的适配器使用的钩子。"""
        return ()
