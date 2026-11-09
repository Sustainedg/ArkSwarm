"""User-defined CLI agent — new agents without writing code.

Users describe how to invoke an arbitrary command-line agent (Aider, a home-grown
DeepSeek harness binary, an internal tool, ...) and how to read its output. This
is the declarative escape hatch that keeps the platform open-ended.
"""
from __future__ import annotations

import json
import shutil
from typing import Any, Iterable

from ..base import (
    AgentContext,
    AgentDefinition,
    AgentRun,
    Availability,
    Capabilities,
    ConfigField,
    FieldType,
    Invocation,
    Runtime,
)
from ..events import AgentEvent, error, log, session, text_delta, tool_call, tool_result, usage
from ..registry import register_agent
from .util import as_list, flatten_content, truthy


@register_agent
class CustomCliAgent(AgentDefinition):
    id = "custom-cli"
    name = "Custom CLI"
    vendor = "Custom"
    description = (
        "Declarative adapter: point it at any CLI agent, describe its arguments "
        "and output shape, and it becomes a first-class role option."
    )
    runtime = Runtime.CLI
    executable = None  # resolved per instance
    auto_provision = False  # a template: nothing to seed until a command is set
    api_key_env = None
    base_url_env = None
    capabilities = Capabilities(
        streaming=True,
        sessions=True,
        native_system_prompt=False,
        tools=True,
        working_directory=True,
    )
    config_fields = (
        ConfigField(
            "command",
            "Executable",
            FieldType.STRING,
            required=True,
            placeholder="aider / my-agent / /opt/bin/deepseek",
            help="Resolved with PATH lookup, exactly like a shell would.",
        ),
        ConfigField(
            "args",
            "Arguments",
            FieldType.STRING_LIST,
            placeholder="--model {model} --message {prompt}",
            help="Placeholders: {prompt} {model} {gateway} {api_key} {workspace} {session}",
        ),
        ConfigField(
            "prompt_mode",
            "Prompt delivery",
            FieldType.SELECT,
            default="stdin",
            options=[
                {"value": "stdin", "label": "stdin (safest)"},
                {"value": "arg", "label": "positional / via {prompt}"},
            ],
        ),
        ConfigField(
            "output_format",
            "Output format",
            FieldType.SELECT,
            default="text",
            options=[
                {"value": "jsonl", "label": "JSON Lines"},
                {"value": "text", "label": "Plain text stream"},
            ],
        ),
        ConfigField(
            "text_path",
            "JSON path to message text",
            FieldType.STRING,
            placeholder="item.text / message.content / delta",
            advanced=True,
        ),
        ConfigField(
            "delta_mode",
            "Treat text path as delta",
            FieldType.BOOLEAN,
            default=True,
            advanced=True,
            help="Off = each JSON line carries the full message so far.",
        ),
        ConfigField(
            "session_path",
            "JSON path to session id",
            FieldType.STRING,
            placeholder="session_id / thread.id",
            advanced=True,
            help="Enables multi-turn once a session id is observed.",
        ),
        ConfigField(
            "tool_path",
            "JSON path to a tool name",
            FieldType.STRING,
            placeholder="tool_call.name",
            advanced=True,
        ),
        ConfigField(
            "tool_result_path",
            "JSON path to tool output",
            FieldType.STRING,
            placeholder="tool_call.output",
            advanced=True,
        ),
        ConfigField(
            "usage_path",
            "JSON path to token usage",
            FieldType.STRING,
            placeholder="usage",
            advanced=True,
        ),
        ConfigField("api_key_env", "API key env var", FieldType.STRING, advanced=True,
                    placeholder="DEEPSEEK_API_KEY"),
        ConfigField("gateway_env", "Gateway env var", FieldType.STRING, advanced=True,
                    placeholder="DEEPSEEK_BASE_URL"),
        ConfigField("env", "Extra environment", FieldType.KEYVALUE, advanced=True),
    )

    # -- introspection -------------------------------------------------------

    def availability(self) -> Availability:
        return Availability(True, detail="resolved from the instance `command` option")

    async def probe(self, ctx: AgentContext) -> tuple[bool, str]:
        command = str(ctx.option("command") or "")
        if not command:
            return False, "no `command` configured"
        path = shutil.which(command) or (command if "/" in command else None)
        if not path:
            return False, f"`{command}` not found on PATH"
        return True, f"resolved to {path}"

    # -- invocation ----------------------------------------------------------

    def _placeholders(self, ctx: AgentContext) -> dict[str, str]:
        return {
            "prompt": ctx.prompt,
            "model": ctx.model or "",
            "gateway": ctx.gateway_url or "",
            "api_key": ctx.api_key or "",
            "workspace": ctx.workspace_dir or "",
            "session": ctx.session_id or "",
            "agent": ctx.agent_name,
            "role": ctx.role_name or "",
        }

    def build_invocation(self, ctx: AgentContext) -> Invocation:
        command = str(ctx.option("command") or "").strip()
        if not command:
            raise ValueError("custom-cli agent needs a `command` option")

        values = self._placeholders(ctx)
        raw_args = as_list(ctx.option("args") or [])
        if not raw_args:
            raw_args = ["-p", "{prompt}"]
        argv = [command] + [_safe_format(arg, values) for arg in raw_args]

        prompt_mode = ctx.option("prompt_mode") or "stdin"
        stdin = None if any("{prompt}" in a for a in raw_args) else None
        if prompt_mode == "stdin":
            stdin = ctx.prompt
        elif not any("{prompt}" in a for a in raw_args):
            argv.append(ctx.prompt)

        env = self.effective_env(ctx)
        if ctx.api_key and ctx.option("api_key_env"):
            env[str(ctx.option("api_key_env"))] = ctx.api_key
        if ctx.gateway_url and ctx.option("gateway_env"):
            env[str(ctx.option("gateway_env"))] = ctx.gateway_url

        return Invocation(argv=argv, env=env, cwd=ctx.workspace_dir, stdin=stdin)

    # -- parsing -------------------------------------------------------------

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:
        line = line.rstrip("\n")
        if not line.strip():
            return
        fmt = run.ctx.option("output_format") or "text"
        if fmt != "jsonl":
            yield text_delta(line + "\n")
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            yield log(line, "debug")
            return
        yield from self._dispatch_json(payload, run)

    def _dispatch_json(self, payload: Any, run: AgentRun) -> Iterable[AgentEvent]:
        options = run.ctx.options
        kind = payload.get("type") if isinstance(payload, dict) else None

        session_path = options.get("session_path")
        if session_path:
            found = _dig(payload, session_path)
            if found and str(found) != str(run.state.get("session")):
                run.state["session"] = found
                yield session(str(found))

        if kind == "error":
            run.state["is_error"] = True
            yield error(flatten_content(payload))
            return

        result_path = options.get("tool_result_path")
        if kind == "tool_result" or (result_path and _dig(payload, result_path) is not None):
            yield tool_result(
                payload.get("id") or payload.get("tool_id") or _dig(payload, "tool_call.id"),
                flatten_content(
                    _dig(payload, result_path) if result_path else payload.get("output") or payload.get("result")
                ),
                bool(payload.get("is_error") or payload.get("error")),
            )
            return

        tool_path = options.get("tool_path")
        if tool_path:
            name = _dig(payload, tool_path)
            if name:
                args = _dig(payload, tool_path.rsplit(".", 1)[0] + ".args") or payload.get("args") or {}
                yield tool_call(
                    str(name),
                    payload.get("id") or _dig(payload, tool_path.rsplit(".", 1)[0] + ".id"),
                    **(args if isinstance(args, dict) else {}),
                )
                return

        if kind in ("result", "usage", "turn.completed", "done"):
            counters = _dig(payload, options.get("usage_path") or "usage")
            if isinstance(counters, dict) and counters:
                yield usage(**counters)
            return

        text_path = options.get("text_path")
        if not text_path:
            return
        value = _dig(payload, text_path)
        if value in (None, ""):
            return
        text = value if isinstance(value, str) else flatten_content(value)
        if truthy(options.get("delta_mode", True)):
            yield text_delta(text)
        else:
            previous = run.state.get("full_text", "")
            if text.startswith(previous):
                incremental = text[len(previous) :]
                run.state["full_text"] = text
                if incremental:
                    yield text_delta(incremental)
            else:
                run.state["full_text"] = text
                yield text_delta(text)

    def finalize(self, run: AgentRun) -> Iterable[AgentEvent]:
        if run.state.get("is_error"):
            return
        return ()


def _safe_format(template: str, values: dict[str, str]) -> str:
    class _Keep(dict):
        def __missing__(self, key: str) -> str:  # leave unknown placeholders intact
            return "{" + key + "}"

    try:
        return template.format_map(_Keep(values))
    except (ValueError, IndexError):
        return template


def _dig(payload: Any, path: str) -> Any:
    cursor = payload
    for part in path.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list) and part.isdigit():
            cursor = cursor[int(part)] if int(part) < len(cursor) else None
        else:
            return None
    return cursor
