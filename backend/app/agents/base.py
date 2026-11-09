"""The contract every agent integration implements.

Adding a brand new agent (Gemini CLI, DeepSeek harness, Aider, ...) means
dropping one module in ``app/agents/builtin`` that defines a subclass of
:class:`AgentDefinition` and decorating it with :func:`register_agent`.
Nothing else in the codebase needs to change.
"""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, ClassVar, Iterable, Sequence

from .events import AgentEvent, EventType


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
    """How the adapter talks to the agent."""

    CLI = "cli"  # spawns a subprocess
    API = "api"  # in-process HTTP calls against an OpenAI-ish endpoint


@dataclass(slots=True)
class ConfigField:
    """Declarative description of one user-editable setting.

    The UI renders these generically, so a new agent ships its own form without
    any frontend work.
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
    sessions: bool = False          # can resume a server-side session/thread
    native_system_prompt: bool = False
    tools: bool = False
    mcp: bool = False
    permission_modes: bool = False
    working_directory: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "streaming": self.streaming,
            "sessions": self.sessions,
            "native_system_prompt": self.native_system_prompt,
            "tools": self.tools,
            "mcp": self.mcp,
            "permission_modes": self.permission_modes,
            "working_directory": self.working_directory,
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
    """A fully resolved process launch (or HTTP target) description."""

    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    stdin: str | None = None
    #: files the runner must materialise before launch (relative -> content)
    files: dict[str, str] = field(default_factory=dict)
    #: which stream the adapter expects its event payloads on
    stream: str = "stdout"


@dataclass(slots=True)
class AgentContext:
    """Everything an adapter needs to build a run, resolved from the database."""

    agent_id: str
    agent_name: str
    #: merged ``options`` blob stored on the agent instance
    options: dict[str, Any] = field(default_factory=dict)
    gateway_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    #: persona
    role_name: str | None = None
    system_prompt: str | None = None
    #: workspace
    workspace_dir: str | None = None
    #: multi-turn support
    session_id: str | None = None
    #: transcripts, injected for agents that cannot resume server-side
    history: list[dict[str, str]] = field(default_factory=list)
    prompt: str = ""

    def option(self, key: str, default: Any = None) -> Any:
        value = self.options.get(key, default)
        return default if value in (None, "") else value


class AgentDefinition(ABC):
    """Static description + runtime behaviour of one agent integration."""

    id: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str] = ""
    vendor: ClassVar[str] = ""
    runtime: ClassVar[Runtime] = Runtime.CLI
    capabilities: ClassVar[Capabilities] = Capabilities()
    config_fields: ClassVar[Sequence[ConfigField]] = ()
    #: env var names used for the standard "bring your own gateway" preset
    api_key_env: ClassVar[str | None] = None
    base_url_env: ClassVar[str | None] = None
    default_gateway: ClassVar[str | None] = None
    default_model: ClassVar[str | None] = None
    docs_url: ClassVar[str | None] = None
    #: the executable that must exist on PATH (CLI adapters only)
    executable: ClassVar[str | None] = None
    #: whether first-run seeding may create an instance of this adapter
    #: (set False for templates that need user input before they can work)
    auto_provision: ClassVar[bool] = True

    # -- introspection -------------------------------------------------------

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
            return Availability(True, detail="in-process")
        path = shutil.which(self.executable)
        if not path:
            return Availability(False, detail=f"`{self.executable}` not found on PATH")
        return Availability(True, version=self._probe_version(path), detail=path)

    def _probe_version(self, path: str) -> str | None:
        try:
            out = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=8, check=False
            )
            return (out.stdout or out.stderr).strip().splitlines()[0][:80] or None
        except Exception:  # noqa: BLE001 - availability probing must never raise
            return None

    def fields_by_key(self) -> dict[str, ConfigField]:
        return {f.key: f for f in self.config_fields}

    def coerce_options(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate/coerce a user supplied options blob against config_fields."""
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
        for key, value in (raw or {}).items():  # keep unknown keys, forward compatible
            coerced.setdefault(key, value)
        return coerced

    def effective_env(self, ctx: AgentContext) -> dict[str, str]:
        """Env vars for the BYO-gateway/api-key convention, plus user extras."""
        env: dict[str, str] = {}
        if self.base_url_env and ctx.gateway_url:
            env[self.base_url_env] = ctx.gateway_url
        if self.api_key_env and ctx.api_key:
            env[self.api_key_env] = ctx.api_key
        for key, value in (ctx.option("env") or {}).items():
            env[str(key)] = str(value)
        return env

    # -- runtime -------------------------------------------------------------

    @abstractmethod
    def build_invocation(self, ctx: AgentContext) -> Invocation:
        """Resolve a run request into a concrete launch description."""

    @abstractmethod
    def parse_line(self, line: str, ctx: AgentContext) -> Iterable[AgentEvent]:
        """Translate one raw output line into zero or more normalized events.

        Adapters are stateful objects created per-run (see :meth:`create_run`),
        so ``parse_line`` may keep counters on ``self``.
        """

    def create_run(self, ctx: AgentContext) -> "AgentRun":
        return AgentRun(self, ctx)

    async def stream(self, ctx: AgentContext) -> AsyncIterator[AgentEvent]:
        """In-process execution path used by ``Runtime.API`` adapters."""
        raise NotImplementedError(f"{self.id} does not implement an in-process stream")
        yield  # pragma: no cover - makes this an async generator

    async def probe(self, ctx: AgentContext) -> tuple[bool, str]:
        """Cheap connectivity/auth check surfaced by the "Test" button."""
        if self.runtime is Runtime.CLI:
            availability = self.availability()
            if not availability.installed:
                return False, availability.detail or "executable missing"
            if not ctx.api_key and not self.api_key_env:
                return True, "CLI present (no API key required)"
            if not ctx.api_key:
                return True, "CLI present (using ambient credentials)"
            return True, f"configuration looks valid ({availability.version or 'cli'})"
        return await self.probe_api(ctx)

    async def probe_api(self, ctx: AgentContext) -> tuple[bool, str]:
        return True, "no probe implemented"


class AgentRun:
    """Binds a stateless :class:`AgentDefinition` to one live execution.

    ``state`` is a scratch dict owned by the run, which lets adapters keep
    parsing state (dedupe flags, tool-call bookkeeping, ...) without any global
    mutable structures.
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
        """Hook for adapters that must flush trailing events."""
        return ()
