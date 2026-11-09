"""Google Gemini CLI adapter."""
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
from .util import as_list, flatten_content

APPROVAL_MODES = ["default", "auto_edit", "yolo"]


@register_agent
class GeminiCliAgent(AgentDefinition):
    id = "gemini-cli"
    name = "Gemini CLI"
    vendor = "Google"
    description = (
        "Google's Gemini CLI agent. Streaming JSONL, checkpointed sessions and "
        "a `GEMINI_SYSTEM_MD` hook for persona injection."
    )
    runtime = Runtime.CLI
    executable = "gemini"
    api_key_env = "GEMINI_API_KEY"
    base_url_env = "GOOGLE_GEMINI_BASE_URL"
    default_gateway = "https://generativelanguage.googleapis.com"
    default_model = "gemini-2.5-pro"
    docs_url = "https://github.com/google-gemini/gemini-cli"
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
            "approval_mode",
            "Approval mode",
            FieldType.SELECT,
            default="yolo",
            options=[
                {"value": "yolo", "label": "yolo (auto-approve tools)"},
                {"value": "auto_edit", "label": "auto_edit"},
                {"value": "default", "label": "default (ask)"},
            ],
        ),
        ConfigField(
            "system_prompt_mode",
            "Persona injection",
            FieldType.SELECT,
            default="system_file",
            options=[
                {"value": "system_file", "label": "GEMINI_SYSTEM_MD system file"},
                {"value": "prepend", "label": "Prepend to the prompt"},
            ],
        ),
        ConfigField("sandbox", "Sandbox", FieldType.BOOLEAN, default=False, advanced=True),
        ConfigField(
            "extensions",
            "Extensions",
            FieldType.STRING_LIST,
            advanced=True,
            placeholder="e.g. gemini-cli-extensions/mcp-toolbox",
        ),
        ConfigField("include_dirs", "Extra workspace dirs", FieldType.STRING_LIST, advanced=True),
        ConfigField("extra_args", "Extra CLI args", FieldType.STRING_LIST, advanced=True),
        ConfigField("env", "Extra environment", FieldType.KEYVALUE, advanced=True),
    )

    # -- invocation ----------------------------------------------------------

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        options = ctx.options
        argv = [self.executable or "gemini", "--output-format", "stream-json"]

        mode = options.get("approval_mode") or "yolo"
        if mode == "yolo":
            argv.append("--yolo")
        elif mode == "auto_edit":
            argv.append("--approval-mode=auto_edit")

        if ctx.model:
            argv += ["-m", ctx.model]
        if ctx.session_id:
            argv += ["--resume", ctx.session_id]
        elif ctx.session_id is not None:
            argv += ["--session-summary", ctx.session_id]
        if options.get("sandbox"):
            argv.append("--sandbox")
        for ext in as_list(options.get("extensions") or []):
            argv += ["--extensions", ext]
        for directory in as_list(options.get("include_dirs") or []):
            argv += ["--include-directories", directory]
        argv += [str(a) for a in as_list(options.get("extra_args") or [])]

        prompt = ctx.prompt
        env = self.effective_env(ctx)
        files: dict[str, str] = {}

        # Gemini CLI injects personas through a system markdown file.
        if ctx.system_prompt:
            if (options.get("system_prompt_mode") or "system_file") == "prepend":
                prompt = f"{ctx.system_prompt}\n\n---\n\n{prompt}"
            else:
                files[".magenta/system.md"] = ctx.system_prompt
                env["GEMINI_SYSTEM_MD"] = "{{RUN_DIR}}/.magenta/system.md"

        # `-p` keeps the prompt out of argv so large contexts cannot overflow it.
        argv += ["-p", ""]
        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=prompt, files=files)

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
        kind = payload.get("type")

        if kind == "init":
            sid = payload.get("session_id")
            if sid:
                run.state["session"] = sid
                yield session(sid)
            if payload.get("model"):
                yield log(f"gemini ready · model={payload['model']}")
            return

        if kind == "message":
            role = payload.get("role")
            text = payload.get("content")
            if not text:
                return
            if role in (None, "assistant"):
                yield text_delta(text)
            return

        if kind in ("tool_call", "tool_use"):
            name = payload.get("tool_name") or payload.get("name") or "tool"
            args = payload.get("parameters") or payload.get("args") or {}
            yield tool_call(name, payload.get("tool_id") or payload.get("id"), **(args if isinstance(args, dict) else {"input": args}))
            return

        if kind == "tool_result":
            yield tool_result(
                payload.get("tool_id") or payload.get("id"),
                flatten_content(payload.get("output") or payload.get("result")),
                bool(payload.get("is_error") or payload.get("error")),
            )
            return

        if kind == "result":
            stats = payload.get("stats") or {}
            if stats:
                counters = {
                    k: v
                    for k, v in stats.items()
                    if k.endswith("_tokens") or k in ("duration_ms", "total_tokens")
                }
                if counters:
                    yield usage(**counters)
            if payload.get("error"):
                run.state["is_error"] = True
                yield error(flatten_content(payload["error"]))
            return

        if kind == "error":
            run.state["is_error"] = True
            yield error(payload.get("message") or flatten_content(payload))
