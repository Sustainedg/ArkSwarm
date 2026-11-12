"""Anthropic Claude Code CLI 适配器。"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..base import (
    AgentContext,
    AgentDefinition,
    AgentRun,
    Capabilities,
    ConfigField,
    FieldType,
    Invocation,
    Runtime,
    RuntimeProvisioner,
)
from ..events import (
    AgentEvent,
    error,
    log,
    session,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
    usage,
)
from ..registry import register_agent
from ..skills import SkillMount, SkillSpec, scan_skill_root
from .util import as_list, flatten_content, truthy


# Claude Code 拒绝以 root 身份使用 bypassPermissions（"cannot be used with root/sudo
# privileges for security reasons"），除非进程声明自己处在一个「有意的沙箱」里 —— 这是内核
# 自带、官方容器场景使用的逃生口 ``IS_SANDBOX=1``。本控制台由 PyPanel 以 root 托管，用户又是
# 显式勾了 bypassPermissions（无人值守），所以这里替它补上；没勾 bypassPermissions 的节点不受影响。


def _is_root() -> bool:
    """当前进程是否以 root 身份运行（real uid，与内核的 ``process.getuid()`` 判定一致）。"""
    getuid = getattr(os, "getuid", None)
    return bool(getuid) and getuid() == 0


def _root_bypass_escape(ctx: AgentContext) -> str | None:
    """root + bypassPermissions 时要给内核声明的 ``IS_SANDBOX`` 值；不需要时返回 ``None``。

    用户在自己的 ``env`` 里显式配了 ``IS_SANDBOX`` 就以用户为准。
    """
    if (ctx.option("permission_mode") or "acceptEdits") != "bypassPermissions":
        return None
    if not _is_root():
        return None
    return str((ctx.option("env") or {}).get("IS_SANDBOX", "1"))


#: Claude Code 自己的系统级配置文件，按顺序合并（后者覆盖前者）。
_SYSTEM_SETTINGS_FILES = ("settings.json", "settings.local.json")

#: Claude Code 自带的内置插件（它们携带 bundled 技能），严格模式下必须显式关闭。
_BUILTIN_PLUGIN_IDS = ("cc-plugin-agents-md@builtin", "cc-plugin-plugin-authoring@builtin")

#: ``disableBundledSkills`` 关不掉、需要逐个用 ``skillOverrides`` 关闭的内置技能。
_RESIDUAL_BUNDLED_SKILLS = ("doctor",)

#: 插件市场目录的扫描深度：plugins/<市场>/plugins/<插件>/skills/<技能>。
_PLUGIN_SKILL_DEPTH = 8


def config_dir(home: str | Path | None = None) -> Path:
    """Claude Code 的配置目录（默认 ``~/.claude``）。"""
    base = Path(home).expanduser() if home else Path.home()
    return base / ".claude"


def read_system_settings(home: str | Path | None = None) -> dict[str, Any]:
    """读取 Claude Code 安装目录下的系统级配置，供界面与运行时对齐真实默认值。"""
    base = Path(home).expanduser() if home else Path.home()
    merged: dict[str, Any] = {}
    merged_env: dict[str, Any] = {}
    for name in _SYSTEM_SETTINGS_FILES:
        try:
            data = json.loads((base / ".claude" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 配置文件缺失或损坏时按「未配置」处理：这是尽力而为的元数据读取，
            # 绝不能因为本机没有 settings.json 就阻断智能体列表或一次真实运行。
            continue
        if not isinstance(data, dict):
            continue
        # env 需要逐键合并（settings.local.json 通常只覆盖其中几项）。
        if isinstance(data.get("env"), dict):
            merged_env.update(data["env"])
        merged.update({k: v for k, v in data.items() if k != "env"})
    if merged_env:
        merged["env"] = merged_env
    return merged


@register_agent
class ClaudeCodeAgent(AgentDefinition):
    id = "claude-code"
    name = "Claude Code"
    vendor = "Anthropic"
    description = (
        "Anthropic 官方编码智能体。以 CLI 子进程运行，支持完整工具调用、"
        "会话续接与原生系统提示词。"
    )
    runtime = Runtime.CLI
    executable = "claude"
    #: 官方通过 npm 分发；平台代劳安装/更新/卸载时用的就是这套命令。
    provisioner = RuntimeProvisioner(
        manager="npm",
        package="@anthropic-ai/claude-code",
        install_argv=["npm", "install", "-g", "@anthropic-ai/claude-code"],
        update_argv=["npm", "install", "-g", "@anthropic-ai/claude-code@latest"],
        uninstall_argv=["npm", "uninstall", "-g", "@anthropic-ai/claude-code"],
        latest_argv=["npm", "view", "@anthropic-ai/claude-code", "version"],
        homepage="https://docs.claude.com/en/docs/claude-code/setup",
    )
    api_key_env = "ANTHROPIC_AUTH_TOKEN"
    base_url_env = "ANTHROPIC_BASE_URL"
    default_gateway = "https://api.anthropic.com"
    default_model = "sonnet"
    docs_url = "https://docs.claude.com/en/docs/claude-code"
    logo = "/icons/kernels/claude.svg"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=True,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
        skills=True,
    )
    config_fields = (
        ConfigField(
            "permission_mode",
            "权限模式",
            FieldType.SELECT,
            default="acceptEdits",
            options=[
                {"value": "acceptEdits", "label": "acceptEdits · 自动接受文件修改"},
                {"value": "auto", "label": "auto · 自动判断"},
                {"value": "manual", "label": "manual · 手动确认"},
                {"value": "plan", "label": "plan · 只做计划不改动"},
                {"value": "dontAsk", "label": "dontAsk · 不询问，直接拒绝需确认的操作"},
                {"value": "bypassPermissions", "label": "bypassPermissions · 跳过全部权限检查"},
            ],
            help=(
                "bypassPermissions 会让智能体无人值守地执行操作，请只在沙箱环境中使用；"
                "以 root 运行时本适配器会自动设置 IS_SANDBOX=1，否则 Claude Code 会拒绝启动。"
            ),
        ),
        ConfigField(
            "auth_style",
            "凭据请求头",
            FieldType.SELECT,
            default="auth_token",
            options=[
                {"value": "auth_token", "label": "Authorization: Bearer（中转网关）"},
                {"value": "api_key", "label": "x-api-key（直连 Anthropic 官方）"},
                {"value": "both", "label": "两个请求头都发送"},
            ],
            help="直连 api.anthropic.com 用 x-api-key；绝大多数中转网关用 Bearer。",
        ),
        ConfigField(
            "system_prompt_mode",
            "人格注入方式",
            FieldType.SELECT,
            default="append",
            options=[
                {"value": "append", "label": "追加到 Claude Code 原有系统提示词之后"},
                {"value": "replace", "label": "完全替换系统提示词"},
            ],
        ),
        ConfigField("partial_messages", "逐字流式输出", FieldType.BOOLEAN, default=True),
        ConfigField(
            "allowed_tools",
            "工具白名单",
            FieldType.STRING_LIST,
            advanced=True,
            placeholder="例如 Bash(git *) Edit Read",
        ),
        ConfigField("disallowed_tools", "工具黑名单", FieldType.STRING_LIST, advanced=True),
        ConfigField("add_dirs", "额外工作目录", FieldType.STRING_LIST, advanced=True),
        ConfigField("fallback_model", "备用模型", FieldType.STRING, advanced=True),
        ConfigField("extra_args", "额外命令行参数", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "额外环境变量", FieldType.KEYVALUE, advanced=True),
    )

    def system_defaults(self) -> dict[str, Any]:
        """从 Claude Code 的真实配置（进程环境变量 + ~/.claude/settings*.json）取默认值。

        这样界面里的网关地址、模型与凭据提示就会反映本机真实配置，而不是适配器里
        写死的占位值；运行时也用同一份配置，避免「界面一套、CLI 另一套」。
        """
        settings = read_system_settings()
        file_env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
        file_env = {str(k): str(v) for k, v in file_env.items() if v not in (None, "")}

        def pick(name: str) -> str | None:
            # 进程环境变量优先级高于配置文件（与 Claude Code 自身一致）。
            return os.environ.get(name) or file_env.get(name) or None

        gateway = pick("ANTHROPIC_BASE_URL")
        model = pick("ANTHROPIC_MODEL") or (str(settings["model"]) if settings.get("model") else None)
        api_key = pick("ANTHROPIC_AUTH_TOKEN") or pick("ANTHROPIC_API_KEY")

        defaults: dict[str, Any] = {}
        if gateway:
            defaults["gateway_url"] = gateway
        if model:
            defaults["model"] = model
        if api_key:
            defaults["api_key"] = api_key
        return defaults

    def discover_skills(self) -> list[SkillSpec]:
        """扫描 Claude Code 的技能根：个人技能 + 插件市场里的插件技能。"""
        root = config_dir()
        specs = scan_skill_root(root / "skills", type_id=self.id, source_kind="user")
        specs += scan_skill_root(
            root / "plugins", type_id=self.id, source_kind="plugin", max_depth=_PLUGIN_SKILL_DEPTH
        )
        return specs

    def skill_mount(self, ctx: AgentContext) -> SkillMount:
        """严格仅分配：只让内核看到角色分配到的技能。

        做法是把运行环境指向一个隔离的配置目录（只放分配到的技能 + 本机
        ``settings*.json``），并关闭内核自带的 bundled 技能与内置插件。
        """
        notes: list[str] = []
        if ctx.workspace_dir:
            extra = Path(ctx.workspace_dir) / ".claude" / "skills"
            if extra.is_dir() and any(extra.iterdir()):
                notes.append(
                    f"工作目录自带的 {extra} 会被内核额外加载，这些技能不在角色分配范围内"
                )
        root = config_dir()
        return SkillMount(
            home_name="claude-home",
            carry=[(str(root / name), name) for name in _SYSTEM_SETTINGS_FILES],
            patch={
                "settings.json": {
                    "enabledPlugins": {plugin_id: False for plugin_id in _BUILTIN_PLUGIN_IDS},
                    "disableBundledSkills": True,
                    "skillOverrides": {slug: "off" for slug in _RESIDUAL_BUNDLED_SKILLS},
                }
            },
            notes=notes,
        )

    # -- 构造启动命令 --------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        argv: list[str] = [
            self.executable or "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if truthy(options.get("partial_messages", True)):
            argv.append("--include-partial-messages")

        if ctx.model:
            argv += ["--model", ctx.model]
        if options.get("fallback_model"):
            argv += ["--fallback-model", str(options["fallback_model"])]

        mode = options.get("permission_mode") or "acceptEdits"
        argv += ["--permission-mode", mode]
        if mode == "bypassPermissions":
            argv.append("--dangerously-skip-permissions")

        if ctx.system_prompt:
            flag = (
                "--system-prompt"
                if options.get("system_prompt_mode") == "replace"
                else "--append-system-prompt"
            )
            argv += [flag, ctx.system_prompt]

        if ctx.session_id:
            argv += ["--resume", ctx.session_id]

        if options.get("allowed_tools"):
            argv += ["--allowedTools", *as_list(options["allowed_tools"])]
        if options.get("disallowed_tools"):
            argv += ["--disallowedTools", *as_list(options["disallowed_tools"])]
        for directory in as_list(ctx.option("add_dirs") or []):
            argv += ["--add-dir", directory]
        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        env = self.effective_env(ctx)
        self._apply_auth_style(env, ctx)
        # 以 root 跑 bypassPermissions 需要内核认作「有意的沙箱」，见 _root_bypass_escape
        sandbox_escape = _root_bypass_escape(ctx)
        if sandbox_escape is not None:
            env.setdefault("IS_SANDBOX", sandbox_escape)
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        env.setdefault("CI", "1")
        if ctx.skill_home_dir:
            # 严格仅分配：内核只从隔离配置目录里的 skills/ 读取技能。
            env["CLAUDE_CONFIG_DIR"] = ctx.skill_home_dir
            env.setdefault("CLAUDE_CODE_DISABLE_BUNDLED_SKILLS", "1")
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=ctx.prompt)

    def _apply_auth_style(self, env: dict[str, str], ctx: AgentContext) -> None:
        """把通用的 api_key 转换成本网关期望的请求头写法。"""
        if not ctx.api_key:
            return
        style = ctx.option("auth_style") or "auth_token"
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if style in ("auth_token", "both"):
            env["ANTHROPIC_AUTH_TOKEN"] = ctx.api_key
        if style in ("api_key", "both"):
            env["ANTHROPIC_API_KEY"] = ctx.api_key

    def new_session_hint(self, ctx: AgentContext) -> str | None:
        return None if ctx.session_id else str(uuid.uuid4())

    # -- 解析输出 ------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        # 只提示一次：root + bypassPermissions 时补了 IS_SANDBOX，否则用户看到内核拒绝启动会一头雾水
        if not run.state.get("_sandbox_noted") and _root_bypass_escape(run.ctx) == "1":
            run.state["_sandbox_noted"] = True
            yield log(
                "当前以 root 运行且勾选了 bypassPermissions：已按 Claude Code 的约定设置 IS_SANDBOX=1",
                "info",
            )
        line = line.strip()
        if not line or not line.startswith("{"):
            if line:
                yield log(line, "debug")
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            yield log(line, "debug")
            return
        yield from self._dispatch(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        kind = payload.get("type")
        sid = payload.get("session_id")
        if sid and sid != run.state.get("session"):
            run.state["session"] = sid
            yield session(sid)

        if kind == "stream_event":
            yield from self._from_stream_event(payload.get("event") or {}, run)
        elif kind == "assistant":
            yield from self._from_assistant(payload.get("message") or {}, run)
        elif kind == "user":
            yield from self._from_tool_results(payload.get("message") or {})
        elif kind == "result":
            yield from self._from_result(payload, run)
        elif kind == "system":
            subtype = payload.get("subtype")
            if subtype == "init":
                run.state["model"] = payload.get("model")
                tools = payload.get("tools") or []
                # 内核只认「平台注入到配置目录里的技能」，把这一轮真实加载到的技能回显出来，
                # 用户才能确认「角色分配」是否生效。
                skills = [str(name) for name in (payload.get("skills") or [])]
                run.state["skills"] = skills
                if tools:
                    preview = "、".join(skills[:8]) + ("…" if len(skills) > 8 else "")
                    detail = f"（{preview}）" if preview else ""
                    yield log(
                        f"会话已就绪 · 模型 {payload.get('model')} · {len(tools)} 个工具"
                        f" · 技能 {len(skills)} 个{detail}"
                    )

    def _from_stream_event(self, event: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            run.state["partial"] = True
            yield text_delta(delta["text"])
        elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
            yield thinking_delta(delta["thinking"])

    def _from_assistant(self, message: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                # 已有逐字增量，不再重复输出整段文本。
                if not run.state.get("partial"):
                    yield text_delta(block["text"])
            elif block.get("type") == "tool_use":
                yield tool_call(
                    block.get("name") or "tool", block.get("id"), **(block.get("input") or {})
                )

    def _from_tool_results(self, message: dict[str, Any]) -> Iterable[AgentEvent]:
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield tool_result(
                    block.get("tool_use_id"),
                    flatten_content(block.get("content")),
                    bool(block.get("is_error")),
                )

    def _from_result(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        if payload.get("usage"):
            # Anthropic 的用量里嵌套了字典，这里只保留数值，界面更干净。
            scalar = {k: v for k, v in payload["usage"].items() if isinstance(v, (int, float))}
            if scalar:
                yield usage(**scalar)
        for src, dst in (("total_cost_usd", "cost_usd"), ("duration_ms", "duration_ms"),
                         ("num_turns", "num_turns")):
            if payload.get(src) is not None:
                run.result[dst] = payload[src]
        run.state["is_error"] = bool(payload.get("is_error"))
        if payload.get("is_error"):
            yield error(payload.get("result") or "Claude Code 报告了一个错误")
