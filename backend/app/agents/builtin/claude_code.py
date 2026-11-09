"""Anthropic Claude Code CLI adapter."""
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
        "Anthropic's official coding agent. Runs as a CLI subprocess with full "
        "tool use, session resumption and native system-prompt support."
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
            "Permission mode",
            FieldType.SELECT,
            default="acceptEdits",
            options=[{"value": m, "label": m} for m in PERMISSION_MODES],
            help="`bypassPermissions` lets the agent act unattended — sandboxes only.",
        ),
        ConfigField(
            "auth_style",
            "Credential header",
            FieldType.SELECT,
            default="auth_token",
            options=[
                {"value": "auth_token", "label": "Authorization: Bearer (gateways)"},
                {"value": "api_key", "label": "x-api-key (Anthropic direct)"},
                {"value": "both", "label": "Send both"},
            ],
            help="Use `x-api-key` for api.anthropic.com, Bearer for most proxies.",
        ),
        ConfigField(
            "system_prompt_mode",
            "Persona injection",
            FieldType.SELECT,
            default="append",
            options=[
                {"value": "append", "label": "Append to Claude Code's prompt"},
                {"value": "replace", "label": "Replace system prompt"},
            ],
        ),
        ConfigField("partial_messages", "Stream partial tokens", FieldType.BOOLEAN, default=True),
        ConfigField(
            "allowed_tools",
            "Allowed tools",
            FieldType.STRING_LIST,
            advanced=True,
            placeholder="Bash(git *) Edit Read",
        ),
        ConfigField("disallowed_tools", "Disallowed tools", FieldType.STRING_LIST, advanced=True),
        ConfigField("add_dirs", "Extra workspace dirs", FieldType.STRING_LIST, advanced=True),
        ConfigField("fallback_model", "Fallback model", FieldType.STRING, advanced=True),
        ConfigField("extra_args", "Extra CLI args", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "Extra environment", FieldType.KEYVALUE, advanced=True),
    )

    # -- invocation ----------------------------------------------------------

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
        """Translate the generic api_key into the header style the gateway wants."""
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

    # -- parsing -------------------------------------------------------------

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
                    yield log(f"session ready · model={payload.get('model')} · {len(tools)} tools")

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
                # Skip the aggregate echo when token deltas already covered it.
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
            # Anthropic nests some counters; keep only scalars for a tidy UI.
            scalar = {k: v for k, v in payload["usage"].items() if isinstance(v, (int, float))}
            if scalar:
                yield usage(**scalar)
        for src, dst in (("total_cost_usd", "cost_usd"), ("duration_ms", "duration_ms"),
                         ("num_turns", "num_turns")):
            if payload.get(src) is not None:
                run.result[dst] = payload[src]
        run.state["is_error"] = bool(payload.get("is_error"))
        if payload.get("is_error"):
            yield error(payload.get("result") or "claude reported an error")
