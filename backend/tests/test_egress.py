"""网络出口过滤与凭据工具结果擦除的测试。

对应 AGENTS.md 铁律 6（凭据只经 ``app.security``）的纵深防御：
网络出口过滤堵住外泄通道，API 内核工具结果擦除补上模型上下文侧的泄露。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from app.agents import AgentContext, get_agent
from app.agents.events import tool_result
from app.credentials import scrub
from app.egress import (
    EgressTransport,
    blocked_message,
    egress_mode,
    host_allowed,
    proxy_env_for_subprocess,
    wrap_client,
)

SECRET = "ghp-super-secret-value-9911"


# --- 出口过滤模式 ----------------------------------------------------------


def test_off_mode_does_not_restrict(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "off")
    from app.settings import get_settings

    get_settings.cache_clear()
    assert egress_mode() == "off"
    assert proxy_env_for_subprocess() == {}
    assert host_allowed("https://evil.example.com") is True


def test_block_mode_rejects_all_hosts(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "block")
    from app.settings import get_settings

    get_settings.cache_clear()
    assert egress_mode() == "block"
    env = proxy_env_for_subprocess()
    assert "HTTP_PROXY" in env and "HTTPS_PROXY" in env
    assert env["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert host_allowed("https://api.openai.com") is False
    assert host_allowed("https://evil.example.com") is False


def test_allowlist_mode_lets_whitelisted_hosts_through(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "allowlist")
    monkeypatch.setenv("MAGENTA_EGRESS_ALLOWED_HOSTS", "api.openai.com,github.com")
    from app.settings import get_settings

    get_settings.cache_clear()
    assert host_allowed("https://api.openai.com/v1/chat/completions") is True
    assert host_allowed("https://github.com/api/v3") is True
    assert host_allowed("https://evil.example.com") is False


def test_allowlist_always_lets_localhost_through(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "allowlist")
    monkeypatch.setenv("MAGENTA_EGRESS_ALLOWED_HOSTS", "")
    from app.settings import get_settings

    get_settings.cache_clear()
    assert host_allowed("http://127.0.0.1:8080") is True
    assert host_allowed("http://localhost:3000") is True


def test_blocked_message_is_user_facing(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "block")
    from app.settings import get_settings

    get_settings.cache_clear()
    msg = blocked_message("https://evil.example.com")
    assert "阻断" in msg


# --- EgressTransport --------------------------------------------------------


async def test_egress_transport_blocks_non_whitelisted(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "block")
    from app.settings import get_settings

    get_settings.cache_clear()

    inner = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    transport = EgressTransport(inner)
    request = httpx.Request("GET", "https://evil.example.com")
    with pytest.raises(httpx.ConnectError, match="阻断"):
        await transport.handle_async_request(request)


async def test_egress_transport_off_mode_passes_through(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "off")
    from app.settings import get_settings

    get_settings.cache_clear()

    inner = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    transport = EgressTransport(inner)
    request = httpx.Request("GET", "https://evil.example.com")
    response = await transport.handle_async_request(request)
    assert response.status_code == 200


async def test_wrap_client_off_mode_returns_unchanged(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "off")
    from app.settings import get_settings

    get_settings.cache_clear()
    client = httpx.AsyncClient()
    assert wrap_client(client) is client


async def test_wrap_client_block_mode_wraps_transport(monkeypatch):
    monkeypatch.setenv("MAGENTA_EGRESS_MODE", "block")
    from app.settings import get_settings

    get_settings.cache_clear()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    wrapped = wrap_client(client)
    assert isinstance(wrapped._transport, EgressTransport)


# --- API 内核工具结果擦除 ----------------------------------------------------


def test_scrub_wipes_credentials_from_tool_output():
    output = f"token={SECRET}\nand some other text"
    cleaned = scrub(output, [SECRET])
    assert SECRET not in cleaned
    assert "***" in cleaned


def _sse(*chunks: dict) -> bytes:
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    return body + b"data: [DONE]\n\n"


async def test_harness_tool_output_is_scrubbed_before_reaching_model(monkeypatch, tmp_path):
    """进程内 API 内核的工具输出回填给模型的 messages 前，凭据值应被擦掉。

    之前只擦了事件流（``scrub_event``），但工具结果原样塞进了 ``messages`` 列表，
    模型在后续推理的上下文里能看到明文。
    """
    definition = get_agent("openai-harness")

    call_chunk = {
        "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                  "function": {"name": "run_shell", "arguments": '{"command": "printenv DEMO_TOKEN"}'}}]}}]
    }
    final_chunks = [
        {"choices": [{"delta": {"content": "done"}}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]

    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            return httpx.Response(200, content=_sse(call_chunk), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, content=_sse(*final_chunks), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        definition,
        "create_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        raising=False,
    )

    # 前面的用例可能改过 egress 模式，这里清缓存确保 off（不限制）
    from app.settings import get_settings
    get_settings.cache_clear()

    ctx = AgentContext(
        agent_id="a1",
        agent_name="Harness",
        gateway_url="https://gateway.example/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
        workspace_dir=str(tmp_path),
        prompt="print the token",
        credentials={"DEMO_TOKEN": SECRET},
    )

    events = [event async for event in definition.stream(ctx)]

    # 事件流里凭据值已被擦掉
    results = [e for e in events if e.type.value == "tool.result"]
    assert results
    assert SECRET not in results[0].payload["output"]
    assert "***" in results[0].payload["output"]

    # 回填给模型的 messages 里也看不到明文
    follow_up = seen_payloads[1]["messages"]
    tool_msg = next(m for m in follow_up if m.get("role") == "tool")
    assert SECRET not in tool_msg["content"]
    assert "***" in tool_msg["content"]
