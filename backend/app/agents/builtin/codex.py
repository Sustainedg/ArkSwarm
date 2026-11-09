"""OpenAI Codex CLI adapter."""
from __future__ import annotations

import json
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

SANDBOX_MODES = ["read-only", "workspace-write", "danger-full-access"]


@register_agent
class CodexAgent(AgentDefinition):
    id = "codex"
    name = "Codex CLI"
    vendor = "OpenAI"
    description = (
        "OpenAI's Codex coding agent. Speaks JSONL over `codex exec --json`, "
        "supports sandbox modes, approval policies and thread resumption."
    )
    runtime = Runtime.CLI
    executable = "codex"
    api_key_env = "OPENAI_API_KEY"
    base_url_env = "OPENAI_BASE_URL"
    default_gateway = "https://api.openai.com/v1"
    default_model = "gpt-5-codex"
    docs_url = "https://developers.openai.com/codex/cli/"
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=False,
        tools=True,
        mcp=True,
        permission_modes=True,
        working_directory=True,
    )
    config_fields = (
        ConfigField(
            "sandbox_mode",
            "Sandbox",
            FieldType.SELECT,
            default="workspace-write",
            options=[{"value": m, "label": m} for m in SANDBOX_MODES],
        ),
        ConfigField(
            "full_auto",
            "Full auto (no approvals)",
            FieldType.BOOLEAN,
            default=False,
            help="Equivalent to `--full-auto`: writes inside the sandbox without asking.",
        ),
        ConfigField(
            "provider_name",
            "Custom provider name",
            FieldType.STRING,
            advanced=True,
            placeholder="e.g. deepseek, openrouter",
            help="When set, Codex is pointed at your gateway through a named model_provider.",
        ),
        ConfigField(
            "wire_api",
            "Provider wire API",
            FieldType.SELECT,
            default="chat",
            options=[{"value": "chat", "label": "chat"}, {"value": "responses", "label": "responses"}],
            advanced=True,
        ),
        ConfigField(
            "resume_style",
            "Multi-turn strategy",
            FieldType.SELECT,
            default="subcommand",
            options=[
                {"value": "subcommand", "label": "codex exec resume <thread> (recommended)"},
                {"value": "transcript", "label": "Replay full transcript each turn"},
                {"value": "none", "label": "Single turn only"},
            ],
            help="Older Codex builds lack `exec resume` — pick transcript for those.",
        ),
        ConfigField(
            "reasoning_effort",
            "Reasoning effort",
            FieldType.SELECT,
            default="",
            options=[{"value": v, "label": v or "default"} for v in ("", "minimal", "low", "medium", "high")],
            advanced=True,
        ),
        ConfigField("add_dirs", "Extra workspace dirs", FieldType.STRING_LIST, advanced=True),
        ConfigField("extra_args", "Extra CLI args", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "Extra environment", FieldType.KEYVALUE, advanced=True),
    )

    # -- invocation ----------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        base = [self.executable or "codex", "exec"]
        argv = list(base)

        resume_style = options.get("resume_style") or "subcommand"
        prompt = ctx.prompt
        if ctx.session_id and resume_style == "subcommand":
            argv += ["resume", ctx.session_id]

        argv.append("--json")
        argv.append("--skip-git-repo-check")

        if ctx.model:
            argv += ["-m", ctx.model]
        sandbox = options.get("sandbox_mode")
        if sandbox:
            argv += ["-s", sandbox]
        if truthy(options.get("full_auto")):
            argv.append("--full-auto")
        if ctx.workspace_dir:
            argv += ["-C", ctx.workspace_dir]

        for key, value in self._config_overrides(ctx).items():
            argv += ["-c", f"{key}={value}"]

        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        # A persona without a native system-prompt flag is folded into the prompt.
        if ctx.system_prompt:
            prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"

        env = self.effective_env(ctx)
        env.setdefault("CODEX_DISABLE_TELEMETRY", "1")
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt)

    def _config_overrides(self, ctx: AgentContext) -> dict[str, str]:
        overrides: dict[str, str] = {}
        provider = ctx.option("provider_name")
        if provider and ctx.gateway_url:
            env_key = self.api_key_env or "OPENAI_API_KEY"
            overrides["model_provider"] = provider
            overrides[f"model_providers.{provider}.name"] = f'"{provider}"'
            overrides[f"model_providers.{provider}.base_url"] = f'"{ctx.gateway_url}"'
            overrides[f"model_providers.{provider}.env_key"] = f'"{env_key}"'
            overrides[f"model_providers.{provider}.wire_api"] = (
                f'"{ctx.option("wire_api") or "chat"}"'
            )
        effort = ctx.option("reasoning_effort")
        if effort:
            overrides["model_reasoning_effort"] = f'"{effort}"'
        return overrides

    def resume_supported(self, ctx: AgentContext) -> bool:
        return (ctx.option("resume_style") or "subcommand") == "subcommand"

    # -- parsing -------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        line = line.strip()
        if not line:
            return
        if not line.startswith("{"):
            yield log(line, "debug")
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            yield log(line, "debug")
            return
        yield from self._dispatch(payload, run)

    def _dispatch(self, payload: dict[str, Any], run: AgentRun) -> Iterable[AgentEvent]:
        if "msg" in payload and isinstance(payload["msg"], dict):  # legacy event shape
            payload = {**payload["msg"], "type": payload["msg"].get("type")}

        kind = payload.get("type")

        if kind == "thread.started":
            thread_id = payload.get("thread_id")
            if thread_id:
                run.state["thread"] = thread_id
                yield session(thread_id)
            return

        if kind in ("item.started", "item.completed", "item.updated"):
            yield from self._from_item(payload.get("item") or {}, kind, run)
            return

        if kind == "turn.completed":
            if payload.get("usage"):
                yield usage(**payload["usage"])
            return

        if kind == "turn.failed":
            yield error(flatten_content(payload.get("error") or "turn failed"))
            run.state["is_error"] = True
            return

        if kind == "error":
            yield error(payload.get("message") or "codex error")
            run.state["is_error"] = True
            return

        if kind == "agent_message":  # legacy
            if payload.get("message"):
                yield text_delta(payload["message"])
            return

    def _from_item(self, item: dict[str, Any], kind: str, run: AgentRun) -> Iterable[AgentEvent]:
        item_type = item.get("type")
        item_id = item.get("id")

        if item_type == "agent_message":
            # `item.completed` carries the full text; deltas arrive via `agent_message_delta`.
            if kind == "item.completed" and item.get("text"):
                if not run.state.get("saw_delta"):
                    yield text_delta(item["text"])
            return

        if item_type == "agent_message_delta":
            if item.get("delta"):
                run.state["saw_delta"] = True
                yield text_delta(item["delta"])
            return

        if item_type in ("reasoning", "reasoning_delta"):
            text = item.get("text") or item.get("delta")
            if text and kind != "item.started":
                yield thinking_delta(text)
            return

        if item_type in ("command_execution", "local_shell_call", "shell"):
            command = item.get("command")
            if kind == "item.started":
                yield tool_call("shell", item_id, command=flatten_content(command))
            elif kind == "item.completed":
                yield tool_result(
                    item_id,
                    flatten_content(item.get("aggregated_output") or item.get("output") or ""),
                    bool(item.get("exit_code")),
                )
            return

        if item_type in ("file_change", "patch_apply", "apply_patch"):
            if kind == "item.completed":
                yield tool_call("patch", item_id, changes=item.get("changes") or item)
            return

        if item_type in ("mcp_tool_call", "web_search", "todo_list"):
            if kind == "item.completed":
                yield tool_call(item_type, item_id, **{k: v for k, v in item.items() if k != "type"})
            return
