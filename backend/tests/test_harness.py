from __future__ import annotations

import json

import httpx
import pytest

from app.agents import AgentContext, get_agent


def sse(*chunks: dict) -> bytes:
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    return body + b"data: [DONE]\n\n"


TOOL_CALL_CHUNK = {
    "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1",
                  "function": {"name": "run_shell", "arguments": '{"command": "echo hi"}'}}]}}]
}
FINAL_CHUNKS = [
    {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
    {"choices": [{"delta": {"content": "all "}}]},
    {"choices": [{"delta": {"content": "done"}}]},
    {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
]


def make_context(tmp_path, **overrides) -> AgentContext:
    base = dict(
        agent_id="a1",
        agent_name="Harness",
        gateway_url="https://gateway.example/v1",
        api_key="sk-test",
        model="gpt-4o-mini",
        system_prompt="You are an analyst.",
        workspace_dir=str(tmp_path),
        prompt="check the workspace",
    )
    base.update(overrides)
    return AgentContext(**base)


@pytest.fixture
def stub_harness(monkeypatch, tmp_path):
    definition = get_agent("openai-harness")
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return httpx.Response(200, content=sse(TOOL_CALL_CHUNK), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, content=sse(*FINAL_CHUNKS), headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        definition,
        "create_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        raising=False,
    )
    return definition, seen


async def test_harness_runs_the_tool_loop_and_streams_text(stub_harness, tmp_path):
    definition, seen = stub_harness
    events = [event async for event in definition.stream(make_context(tmp_path))]
    kinds = [event.type.value for event in events]

    assert kinds[0] == "tool.call"
    assert "tool.result" in kinds
    assert "thinking.delta" in kinds
    text = "".join(e.text or "" for e in events if e.type.value == "message.delta")
    assert text == "all done"
    totals = {k: v for e in events if e.type.value == "usage" for k, v in e.payload.items()}
    assert totals == {"prompt_tokens": 7, "completion_tokens": 3}

    # shell 工具确实在工作目录内执行了
    results = [e for e in events if e.type.value == "tool.result"]
    assert "hi" in results[0].payload["output"]
    assert results[0].payload["is_error"] is False

    # 第二次请求会重放助手的工具调用与工具输出
    follow_up = seen[1]["messages"]
    assert follow_up[-2]["role"] == "assistant" and follow_up[-2]["tool_calls"][0]["id"] == "call_1"
    assert follow_up[-1] == {"role": "tool", "tool_call_id": "call_1", "content": results[0].payload["output"]}
    assert seen[0]["stream_options"]["include_usage"] is True
    assert seen[0]["model"] == "gpt-4o-mini"


async def test_harness_reports_gateway_errors(stub_harness, monkeypatch, tmp_path):
    definition, _ = stub_harness
    monkeypatch.setattr(
        definition,
        "create_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(401, text="bad key"))
        ),
        raising=False,
    )
    events = [event async for event in definition.stream(make_context(tmp_path))]
    assert events[-1].type.value == "error"
    assert "401" in events[-1].text


async def test_harness_blocks_workspace_escapes(tmp_path):
    from app.agents.builtin.harness_tools import ToolError, WorkspaceTools

    tools = WorkspaceTools(root=str(tmp_path))
    with pytest.raises(ToolError):
        await tools.read_file("../../etc/passwd")

    (tmp_path / "note.txt").write_text("hello")
    assert "hello" in await tools.read_file("note.txt")
    assert "note.txt" in await tools.list_dir(".")


async def test_harness_can_disable_shell(tmp_path):
    definition = get_agent("openai-harness")
    context = make_context(tmp_path, options={"allow_shell": False})
    assert definition.tool_specs(context) is not None
    from app.agents.builtin.harness_tools import ToolError, WorkspaceTools

    tools = WorkspaceTools(root=str(tmp_path), allow_shell=False)
    with pytest.raises(ToolError):
        await tools.run_shell("echo hi")
