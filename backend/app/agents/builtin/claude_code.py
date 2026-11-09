"""Anthropic Claude Code CLI 适配器。"""
from __future__ import annotations

import json
import uuid
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
from .util import as_list, flatten_content, truthy

PERMISSION_MODES = ["acceptEdits", "auto", "manual", "plan", "dontAsk", "bypassPermissions"]


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
    api_key_env = "ANTHROPIC_AUTH_TOKEN"
    base_url_env = "ANTHROPIC_BASE_URL"
    default_gateway = "https://api.anthropic.com"
    default_model = "sonnet"
    docs_url = "https://docs.claude.com/en/docs/claude-code"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=True,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
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
            help="bypassPermissions 会让 agent 无人值守地执行操作，请只在沙箱环境中使用。",
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
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        env.setdefault("CI", "1")
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
                if tools:
                    yield log(f"会话已就绪 · 模型 {payload.get('model')} · {len(tools)} 个工具")

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
