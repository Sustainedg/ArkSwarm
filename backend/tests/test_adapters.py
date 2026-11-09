from __future__ import annotations

import json

from app.agents import AgentContext, get_agent, known_ids
from app.agents.base import AgentRun


def ctx(**overrides) -> AgentContext:
    base = dict(
        agent_id="a1",
        agent_name="demo",
        gateway_url="https://gateway.example/v1",
        api_key="sk-secret",
        model="some-model",
        role_name="Tester",
        system_prompt="You are a tester.",
        workspace_dir="/tmp/ws",
        prompt="hello world",
    )
    base.update(overrides)
    return AgentContext(**base)


def test_registry_exposes_first_party_adapters():
    for expected in ("claude-code", "codex", "gemini-cli", "deepseek-harness", "custom-cli"):
        assert expected in known_ids()


def test_claude_code_invocation_and_auth_styles():
    definition = get_agent("claude-code")
    invocation = definition.build_invocation(ctx())
    assert invocation.argv[:4] == ["claude", "-p", "--output-format", "stream-json"]
    assert "--append-system-prompt" in invocation.argv
    assert invocation.stdin == "hello world"
    assert invocation.env["ANTHROPIC_BASE_URL"] == "https://gateway.example/v1"
    assert invocation.env["ANTHROPIC_AUTH_TOKEN"] == "sk-secret"
    assert "ANTHROPIC_API_KEY" not in invocation.env

    keyed = get_agent("claude-code").build_invocation(
        ctx(options={"auth_style": "api_key"}, gateway_url=None)
    )
    assert keyed.env["ANTHROPIC_API_KEY"] == "sk-secret"
    assert "ANTHROPIC_AUTH_TOKEN" not in keyed.env


def test_claude_code_stream_parsing_dedupes_partial_text():
    definition = get_agent("claude-code")
    context = ctx()
    run = definition.create_run(context)
    lines = [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "sonnet", "tools": ["Bash"]},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "result", "subtype": "success", "session_id": "s1", "usage": {"input_tokens": 3, "output_tokens": 4}, "total_cost_usd": 0.01},
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    kinds = [e.type.value for e in events]
    assert kinds.count("session") == 1
    assert run.emitted_text == "Hello"  # 不应重复输出整段文本
    assert "tool.call" in kinds and "tool.result" in kinds and "usage" in kinds
    assert run.session_id == "s1"


def test_codex_invocation_uses_named_provider_and_resume():
    definition = get_agent("codex")
    invocation = definition.build_invocation(
        ctx(options={"provider_name": "deepseek", "sandbox_mode": "workspace-write"}, session_id="tid-1")
    )
    assert invocation.argv[:3] == ["codex", "exec", "resume"]
    assert "tid-1" in invocation.argv
    assert "--json" in invocation.argv
    joined = " ".join(invocation.argv)
    assert "model_providers.deepseek.base_url=\"https://gateway.example/v1\"" in joined
    assert invocation.env["OPENAI_API_KEY"] == "sk-secret"


def test_codex_parses_modern_and_legacy_events():
    definition = get_agent("codex")
    run = definition.create_run(ctx())
    run.state["saw_delta"] = True
    lines = [
        {"type": "thread.started", "thread_id": "th1"},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "full text"}},
        {"type": "item.started", "item": {"id": "i2", "type": "command_execution", "command": "ls -la"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "aggregated_output": "files", "exit_code": 0}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 6}},
        {"id": "0", "msg": {"type": "agent_message", "message": "legacy"}},
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    types = [e.type.value for e in events]
    assert "session" in types and "tool.call" in types and "tool.result" in types and "usage" in types
    assert run.emitted_text == "legacy"  # 已有增量时抑制网关补发的整段文本
    assert run.session_id == "th1"


def test_gemini_invocation_writes_system_file():
    definition = get_agent("gemini-cli")
    invocation = definition.build_invocation(ctx(session_id="sess-9"))
    assert "--resume" in invocation.argv and "sess-9" in invocation.argv
    assert invocation.files[".magenta/system.md"] == "You are a tester."
    assert invocation.env["GEMINI_SYSTEM_MD"] == "{{RUN_DIR}}/.magenta/system.md"
    assert invocation.env["GEMINI_API_KEY"] == "sk-secret"


def test_custom_cli_declarative_args_and_plain_text():
    definition = get_agent("custom-cli")
    context = ctx(
        options={"command": "echo", "args": ["-n", "{model}:{prompt}"], "output_format": "text"},
        model="m1",
    )
    run: AgentRun = definition.create_run(context)
    invocation = run.build_invocation()
    assert invocation.argv == ["echo", "-n", "m1:hello world"]
    events = list(run.feed("first"))
    assert events[0].text.startswith("first")


def test_custom_cli_placeholder_survives_unknown_keys():
    definition = get_agent("custom-cli")
    context = ctx(options={"command": "tool", "args": ["{unknown}"]})
    invocation = definition.create_run(context).build_invocation()
    assert invocation.argv == ["tool", "{unknown}"]


def test_harness_builds_transcript_messages():
    definition = get_agent("deepseek-harness")
    context = ctx(history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
    messages = definition.build_messages(context)
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert definition.chat_completions_url(context) == "https://gateway.example/v1/chat/completions"


def _write_claude_settings(tmp_path, settings: dict, local: dict | None = None) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    if local is not None:
        (claude_dir / "settings.local.json").write_text(json.dumps(local), encoding="utf-8")


def _isolate_anthropic_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_claude_code_reads_real_system_config(monkeypatch, tmp_path):
    _write_claude_settings(
        tmp_path,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "http://10.0.0.1:3065",
                "ANTHROPIC_AUTH_TOKEN": "sk-local-secret",
                "ANTHROPIC_API_KEY": "",  # 空值应被忽略
            },
            "model": "deepseek-v4.1-flash",
        },
        local={"env": {"ANTHROPIC_MODEL": "local-model"}},
    )
    _isolate_anthropic_env(monkeypatch, tmp_path)

    defaults = get_agent("claude-code").system_defaults()
    assert defaults["gateway_url"] == "http://10.0.0.1:3065"
    assert defaults["model"] == "local-model"  # settings.local.json 覆盖 settings.json
    assert defaults["api_key"] == "sk-local-secret"


def test_unmodified_fields_fall_back_to_real_system_config(monkeypatch, tmp_path):
    _write_claude_settings(
        tmp_path,
        {"env": {"ANTHROPIC_BASE_URL": "http://gw.local:1", "ANTHROPIC_MODEL": "real-model"}},
    )
    _isolate_anthropic_env(monkeypatch, tmp_path)
    definition = get_agent("claude-code")

    # 等于内置默认值 / 为空 → 视为未自定义，回落到本机真实配置
    assert definition.resolve_gateway(definition.default_gateway) == "http://gw.local:1"
    assert definition.resolve_model(definition.default_model) == "real-model"
    assert definition.resolve_gateway(None) == "http://gw.local:1"
    # 显式覆盖的值保持不变
    assert definition.resolve_gateway("https://mine.example") == "https://mine.example"
    assert definition.resolve_model("mine-model") == "mine-model"


def test_adapters_without_real_config_keep_their_defaults(monkeypatch, tmp_path):
    _isolate_anthropic_env(monkeypatch, tmp_path)
    definition = get_agent("codex")
    assert definition.system_defaults() == {}
    assert definition.resolve_gateway(definition.default_gateway) == definition.default_gateway
