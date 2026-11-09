"""In-process agent harness for any OpenAI-compatible chat endpoint.

This is the adapter family that covers model providers without an official CLI:
DeepSeek, Kimi, GLM, Qwen, OpenRouter, vLLM/Ollama, ...
The "harness" is the agent loop itself — streaming completions plus a sandboxed
tool loop — so a plain chat model behaves like a coding agent.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterable

import httpx

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
from ..events import (
    AgentEvent,
    error,
    log,
    text_delta,
    thinking_delta,
    tool_call,
    tool_result,
    usage,
)
from ..registry import register_agent
from .harness_tools import ToolError, WorkspaceTools

DEFAULT_MAX_STEPS = 12


class OpenAIHarnessBase(AgentDefinition):
    runtime = Runtime.API
    executable = None
    capabilities = Capabilities(
        streaming=True,
        sessions=False,  # transcript is replayed from the database
        native_system_prompt=True,
        tools=True,
        mcp=False,
        permission_modes=False,
        working_directory=True,
    )
    config_fields = (
        ConfigField(
            "max_steps",
            "Max agent steps",
            FieldType.NUMBER,
            default=DEFAULT_MAX_STEPS,
            help="Upper bound on tool-calling round trips per user turn.",
        ),
        ConfigField("allow_shell", "Enable shell tool", FieldType.BOOLEAN, default=True),
        ConfigField(
            "shell_timeout",
            "Shell timeout (s)",
            FieldType.NUMBER,
            default=120,
            advanced=True,
        ),
        ConfigField(
            "temperature",
            "Temperature",
            FieldType.NUMBER,
            default=0.3,
            advanced=True,
        ),
        ConfigField("max_tokens", "Max output tokens", FieldType.NUMBER, advanced=True),
        ConfigField(
            "extra_headers",
            "Extra HTTP headers",
            FieldType.KEYVALUE,
            advanced=True,
            help="Useful for gateways that require routing headers.",
        ),
        ConfigField(
            "extra_body",
            "Extra request body",
            FieldType.KEYVALUE,
            advanced=True,
        ),
    )

    def availability(self) -> Availability:
        return Availability(True, detail="in-process (no local executable)")

    def create_client(self) -> httpx.AsyncClient:
        """Overridable so tests (and custom transports) can inject an httpx client."""
        return httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=20.0))

    # -- HTTP ----------------------------------------------------------------

    def chat_completions_url(self, ctx: AgentContext) -> str:
        base = (ctx.gateway_url or self.default_gateway or "").rstrip("/")
        if not base:
            raise RuntimeError("gateway URL is required for API agents")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def build_messages(self, ctx: AgentContext) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        for turn in ctx.history:
            role = turn.get("role")
            if role in ("user", "assistant") and turn.get("content"):
                messages.append({"role": role, "content": turn["content"]})
        messages.append({"role": "user", "content": ctx.prompt})
        return messages

    def request_payload(self, ctx: AgentContext, messages: list[dict[str, Any]]) -> dict[str, Any]:
        options = ctx.options
        payload: dict[str, Any] = {
            "model": ctx.model or self.default_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "tools": self.tool_specs(ctx),
        }
        if options.get("temperature") is not None:
            payload["temperature"] = float(options["temperature"])
        if options.get("max_tokens"):
            payload["max_tokens"] = int(options["max_tokens"])
        for key, value in (options.get("extra_body") or {}).items():
            payload[key] = value
        return payload

    def tool_specs(self, ctx: AgentContext) -> list[dict[str, Any]] | None:
        from .harness_tools import TOOL_SPECS

        return list(TOOL_SPECS)

    def http_headers(self, ctx: AgentContext) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if ctx.api_key:
            headers["Authorization"] = f"Bearer {ctx.api_key}"
        for key, value in (ctx.option("extra_headers") or {}).items():
            headers[str(key)] = str(value)
        return headers

    # -- agent loop ----------------------------------------------------------

    async def stream(self, ctx: AgentContext) -> AsyncIterator[AgentEvent]:
        messages = self.build_messages(ctx)
        workspace = WorkspaceTools(
            root=ctx.workspace_dir or ".",
            allow_shell=bool(ctx.option("allow_shell", True)),
            timeout=int(ctx.option("shell_timeout", 120) or 120),
        )
        max_steps = int(ctx.option("max_steps", DEFAULT_MAX_STEPS) or DEFAULT_MAX_STEPS)
        totals: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        url = self.chat_completions_url(ctx)

        async with self.create_client() as client:
            for step in range(max_steps + 1):
                payload = self.request_payload(ctx, messages)
                if step == max_steps:
                    payload.pop("tools", None)
                collected: dict[str, Any] = {"content": "", "reasoning": "", "tool_calls": []}
                try:
                    async for event in self._stream_request(client, url, payload, ctx, collected, totals):
                        yield event
                except httpx.HTTPStatusError as exc:
                    body = exc.response.text[:500]
                    yield error(f"gateway returned {exc.response.status_code}: {body}")
                    return
                except httpx.HTTPError as exc:
                    yield error(f"gateway request failed: {exc}")
                    return

                calls = collected["tool_calls"]
                if not calls:
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": collected["content"] or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for c in calls
                        ],
                    }
                )
                async for event in self._execute_tools(calls, workspace, messages):
                    yield event

        if totals["prompt_tokens"] or totals["completion_tokens"]:
            yield usage(**totals)

    async def _stream_request(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict[str, Any],
        ctx: AgentContext,
        collected: dict[str, Any],
        totals: dict[str, int],
    ) -> AsyncIterator[AgentEvent]:
        async with client.stream("POST", url, json=payload, headers=self.http_headers(ctx)) as response:
            if response.status_code >= 400:
                await response.aread()
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    totals["prompt_tokens"] += chunk["usage"].get("prompt_tokens", 0) or 0
                    totals["completion_tokens"] += chunk["usage"].get("completion_tokens", 0) or 0
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        collected["reasoning"] += delta["reasoning_content"]
                        yield thinking_delta(delta["reasoning_content"])
                    if delta.get("content"):
                        collected["content"] += delta["content"]
                        yield text_delta(delta["content"])
                    for call in delta.get("tool_calls") or []:
                        _merge_tool_call(collected["tool_calls"], call)

    async def _execute_tools(
        self,
        calls: list[dict[str, Any]],
        workspace: WorkspaceTools,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[AgentEvent]:
        for call in calls:
            name = call["name"] or "tool"
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            yield tool_call(name, call["id"], **arguments)
            try:
                output = await workspace.call(name, arguments)
                is_error = False
            except ToolError as exc:
                output, is_error = str(exc), True
            except TypeError as exc:
                output, is_error = f"bad arguments: {exc}", True
            yield tool_result(call["id"], output, is_error)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": output if not is_error else f"ERROR: {output}",
                }
            )
        return

    async def probe_api(self, ctx: AgentContext) -> tuple[bool, str]:
        url = self.chat_completions_url(ctx)
        models_url = url[: -len("/chat/completions")] + "/models"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(models_url, headers=self.http_headers(ctx))
            if response.status_code < 400:
                return True, f"gateway reachable ({response.status_code})"
            return False, f"gateway responded {response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            return False, f"cannot reach gateway: {exc}"

    # unused for API agents, kept so the base contract stays satisfied
    def build_invocation(self, ctx: AgentContext) -> Invocation:  # pragma: no cover
        raise NotImplementedError("API harness agents do not spawn processes")

    def parse_line(self, line: str, run: AgentRun) -> Iterable[AgentEvent]:  # pragma: no cover
        return ()


def _merge_tool_call(bucket: list[dict[str, Any]], chunk: dict[str, Any]) -> None:
    """OpenAI streams tool calls in fragments keyed by index."""
    index = chunk.get("index", 0)
    while len(bucket) <= index:
        bucket.append({"id": "", "name": "", "arguments": ""})
    slot = bucket[index]
    if chunk.get("id"):
        slot["id"] = chunk["id"]
    function = chunk.get("function") or {}
    if function.get("name"):
        slot["name"] = function["name"]
    if function.get("arguments"):
        slot["arguments"] += function["arguments"]


@register_agent
class OpenAICompatibleHarness(OpenAIHarnessBase):
    id = "openai-harness"
    name = "OpenAI-compatible harness"
    vendor = "Generic"
    description = (
        "Any OpenAI-compatible /chat/completions endpoint driven by a built-in "
        "tool loop. Covers OpenRouter, vLLM, Ollama, Kimi, GLM and friends."
    )
    api_key_env = "OPENAI_API_KEY"
    base_url_env = "OPENAI_BASE_URL"
    default_gateway = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"


@register_agent
class DeepSeekHarness(OpenAIHarnessBase):
    id = "deepseek-harness"
    name = "DeepSeek harness"
    vendor = "DeepSeek"
    description = (
        "DeepSeek chat/reasoner models wrapped in the shared tool loop. Shows how "
        "a new model provider becomes an agent without any new plumbing."
    )
    api_key_env = "DEEPSEEK_API_KEY"
    base_url_env = "DEEPSEEK_BASE_URL"
    default_gateway = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"
    docs_url = "https://api-docs.deepseek.com/"
    config_fields = OpenAIHarnessBase.config_fields + (
        ConfigField(
            "model_family",
            "Model family",
            FieldType.SELECT,
            default="deepseek-chat",
            options=[
                {"value": "deepseek-chat", "label": "deepseek-chat (V3)"},
                {"value": "deepseek-reasoner", "label": "deepseek-reasoner (R1)"},
            ],
        ),
    )
