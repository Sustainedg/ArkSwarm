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
        system_prompt="You are a tester.",
        workspace_dir="/tmp/ws",
        prompt="hello world",
    )
    base.update(overrides)
    return AgentContext(**base)


def test_registry_exposes_first_party_adapters():
    for expected in ("claude-code", "codex", "gemini-cli", "openai-harness", "custom-cli", "hermes-agent"):
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

    # 网关声明 x-api-key（直连 Anthropic 官方）时换用 API_KEY 请求头
    keyed = get_agent("claude-code").build_invocation(ctx(auth_style="x-api-key"))
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
        ctx(
            options={
                "transport": "exec",
                "provider_name": "mygateway",
                "sandbox_mode": "workspace-write",
            },
            session_id="tid-1",
        )
    )
    assert invocation.argv[:3] == ["codex", "exec", "resume"]
    assert "tid-1" in invocation.argv
    assert "--json" in invocation.argv
    joined = " ".join(invocation.argv)
    assert "model_providers.mygateway.base_url=\"https://gateway.example/v1\"" in joined
    assert invocation.env["OPENAI_API_KEY"] == "sk-secret"


def test_codex_gateway_goes_through_a_named_provider():
    """自定义网关必须落到具名 model_provider 上 —— 新版 Codex 已不再读 ``OPENAI_BASE_URL``。

    同时不下发 ``wire_api``：新版只剩 responses、旧版默认 chat，写死任何一边都会在另一边
    直接报错；``provider_name`` 留空时用内置名 ``magenta``。
    """
    definition = get_agent("codex")
    # 存量智能体里由旧默认值写下的 wire_api=chat 必须被忽略，否则新版 Codex 会拒绝启动。
    invocation = definition.build_invocation(
        ctx(options={"transport": "exec", "wire_api": "chat"})
    )
    joined = " ".join(invocation.argv)
    assert "model_provider=magenta" in joined
    assert 'model_providers.magenta.base_url="https://gateway.example/v1"' in joined
    assert 'model_providers.magenta.env_key="OPENAI_API_KEY"' in joined
    assert "wire_api" not in joined

    # 没走网关（官方默认 / 本机登录态）时不注入任何 provider，交给 Codex 自己的配置。
    bare = definition.build_invocation(ctx(options={"transport": "exec"}, gateway_url=None))
    assert "model_provider" not in " ".join(bare.argv)


def test_codex_disables_the_metrics_export_that_stalls_shutdown():
    """退出前的 OTEL 指标导出连不上分析后端会干等满超时（内网约 10 秒），必须关掉。

    未知的 ``-c`` 键在旧版 Codex 上会被忽略，所以这个开关对老版本是安全的。
    """
    definition = get_agent("codex")
    invocation = definition.build_invocation(ctx(options={"transport": "exec"}))
    joined = " ".join(invocation.argv)
    assert 'otel.metrics_exporter="none"' in joined
    # 走官方默认（本机登录态）时同样要关，否则一样会卡。
    assert 'otel.metrics_exporter="none"' in " ".join(
        definition.build_invocation(ctx(options={"transport": "exec"}, gateway_url=None)).argv
    )


def test_codex_invocation_uses_flags_the_cli_still_accepts():
    """`codex exec` 0.4x 起不再有 --full-auto；`exec resume` 也不认 -s/-C（只认配置覆盖）。"""
    definition = get_agent("codex")
    options = {"transport": "exec", "full_auto": True, "sandbox_mode": "danger-full-access"}

    fresh = definition.build_invocation(ctx(options=options))
    assert "--full-auto" not in fresh.argv
    assert 'approval_policy="never"' in fresh.argv
    assert fresh.argv[fresh.argv.index("-s") + 1] == "danger-full-access"

    resumed = definition.build_invocation(ctx(options=options, session_id="tid-1"))
    assert resumed.argv[:3] == ["codex", "exec", "resume"]
    assert "-s" not in resumed.argv and "-C" not in resumed.argv
    assert 'sandbox_mode="danger-full-access"' in resumed.argv
    assert 'approval_policy="never"' in resumed.argv


def test_codex_parses_modern_and_legacy_events():
    definition = get_agent("codex")
    run = definition.create_run(ctx(options={"transport": "exec"}))
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
    definition = get_agent("openai-harness")
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
            "model": "my-model-v1",
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


def test_claude_code_root_bypass_sets_sandbox_env(monkeypatch):
    """以 root 勾 bypassPermissions 时必须声明 IS_SANDBOX，否则内核直接拒绝启动。"""
    from app.agents.builtin import claude_code

    definition = get_agent("claude-code")

    monkeypatch.setattr(claude_code, "_is_root", lambda: True)
    invocation = definition.build_invocation(ctx(options={"permission_mode": "bypassPermissions"}))
    assert "--dangerously-skip-permissions" in invocation.argv
    assert invocation.env["IS_SANDBOX"] == "1"
    # 运行日志里要有一次明示，否则用户看到内核拒绝启动会一头雾水
    run = definition.create_run(ctx(options={"permission_mode": "bypassPermissions"}))
    notes = [e for e in run.feed("") if e.type.value == "log" and "IS_SANDBOX" in (e.text or "")]
    assert len(notes) == 1

    # 常规权限模式不受影响
    plain = definition.build_invocation(ctx(options={"permission_mode": "acceptEdits"}))
    assert "IS_SANDBOX" not in plain.env

    # 非 root 也不用补
    monkeypatch.setattr(claude_code, "_is_root", lambda: False)
    assert "IS_SANDBOX" not in definition.build_invocation(
        ctx(options={"permission_mode": "bypassPermissions"})
    ).env

    # 用户自己配了 env.IS_SANDBOX 就以用户为准
    monkeypatch.setattr(claude_code, "_is_root", lambda: True)
    custom = definition.build_invocation(
        ctx(options={"permission_mode": "bypassPermissions", "env": {"IS_SANDBOX": "0"}})
    )
    assert custom.env["IS_SANDBOX"] == "0"


# --- Codex app-server（JSON-RPC）-----------------------------------------


def _app_server_run(**overrides):
    """建一个走 app-server 的 Codex 运行（默认选项里只覆盖 transport）。"""
    definition = get_agent("codex")
    options = {"transport": "app-server", "sandbox_mode": "workspace-write"}
    options.update(overrides.pop("options", {}))
    return definition, definition.create_run(ctx(options=options, **overrides))


def _request(run) -> dict:
    outbound = run.take_outbound()
    assert len(outbound) == 1, outbound
    return json.loads(outbound[0])


def test_codex_app_server_invocation_speaks_json_rpc_over_stdio():
    """app-server 模式：argv 是常驻服务，stdin 只放第一帧 initialize 且保持打开。"""
    definition = get_agent("codex")
    invocation = definition.build_invocation(ctx())
    assert invocation.argv[:3] == ["codex", "app-server", "--listen"]
    assert "stdio://" in invocation.argv
    assert invocation.stdin_hold is True
    assert "--json" not in invocation.argv
    frame = json.loads(invocation.stdin)
    assert frame["method"] == "initialize" and frame["id"] == 1
    assert frame["params"]["clientInfo"]["name"] == "magenta-agents"

    # 逐字流式来自 app-server；exec 仍是可选的老路径（写完 stdin 就关）。
    legacy = definition.build_invocation(ctx(options={"transport": "exec"}))
    assert legacy.argv[:2] == ["codex", "exec"]
    assert legacy.stdin_hold is False


def test_codex_app_server_handshake_then_streams_deltas():
    definition, run = _app_server_run()

    # initialize 的响应到齐后，适配器把 thread/start 排进队列（由运行层代写）。
    assert list(run.feed(json.dumps({"id": 1, "result": {"userAgent": "x"}}))) == []
    thread_request = _request(run)
    assert thread_request["method"] == "thread/start"
    assert thread_request["params"]["cwd"] == "/tmp/ws"
    assert thread_request["params"]["model"] == "some-model"
    assert thread_request["params"]["sandbox"] == "workspace-write"
    assert thread_request["params"]["approvalPolicy"] == "never"

    events = list(run.feed(json.dumps({"id": 2, "result": {"thread": {"id": "th-1"}}})))
    assert [e.type.value for e in events] == ["session"]
    assert run.session_id == "th-1"
    turn_request = _request(run)
    assert turn_request["method"] == "turn/start"
    assert turn_request["params"]["threadId"] == "th-1"
    assert turn_request["params"]["input"] == [{"type": "text", "text": "hello world"}]

    # 逐字增量：一个 delta 一个事件。
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"itemId": "m1", "delta": "你"},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["message.delta"]
    assert run.emitted_text == "你"

    # 已有增量的条目不再补发整段文本，避免内容翻倍。
    assert (
        list(
            run.feed(
                json.dumps(
                    {
                        "method": "item/completed",
                        "params": {"item": {"type": "agentMessage", "id": "m1", "text": "你好"}},
                    }
                )
            )
        )
        == []
    )

    # 思考走 summaryTextDelta。
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"itemId": "r1", "delta": "想一下"},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["thinking.delta"]

    # token 用量是 camelCase，翻译成与 exec 一致的键名。
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "last": {
                                "inputTokens": 5,
                                "cachedInputTokens": 1,
                                "cacheWriteInputTokens": 0,
                                "outputTokens": 6,
                                "reasoningOutputTokens": 2,
                                "totalTokens": 11,
                            }
                        }
                    },
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["usage"]
    assert events[0].payload["input_tokens"] == 5
    assert events[0].payload["total_tokens"] == 11

    # 本轮结束：适配器声明收工，运行层据此关 stdin。
    assert (
        list(
            run.feed(
                json.dumps(
                    {"method": "turn/completed", "params": {"turn": {"id": "t1", "status": "completed"}}}
                )
            )
        )
        == []
    )
    assert run.done is True


def test_codex_app_server_falls_back_to_the_finished_message():
    """服务端没给增量（旧版 app-server）时，item/completed 要补上整段文本。"""
    _, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {"item": {"type": "agentMessage", "id": "m9", "text": "完整回复"}},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["message.delta"]
    assert run.emitted_text == "完整回复"


def test_codex_app_server_maps_tool_items():
    _, run = _app_server_run()
    started = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/started",
                    "params": {
                        "item": {"type": "commandExecution", "id": "c1", "command": "ls -la"}
                    },
                }
            )
        )
    )
    assert [e.type.value for e in started] == ["tool.call"]
    assert started[0].name == "shell"

    done = list(
        run.feed(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "id": "c1",
                            "status": "completed",
                            "exitCode": 0,
                            "aggregatedOutput": "files",
                        }
                    },
                }
            )
        )
    )
    assert [e.type.value for e in done] == ["tool.result"]
    assert done[0].payload["output"] == "files"
    assert done[0].payload["is_error"] is False


def test_codex_app_server_resumes_the_snapshot_thread():
    definition, run = _app_server_run(session_id="th-old")
    list(run.feed(json.dumps({"id": 1, "result": {}})))
    request = _request(run)
    assert request["method"] == "thread/resume"
    assert request["params"]["threadId"] == "th-old"

    # 选「每轮重放完整对话记录」时不能恢复服务端线程，否则历史会重复。
    _, replayed = _app_server_run(
        session_id="th-old", options={"resume_style": "transcript"}
    )
    list(replayed.feed(json.dumps({"id": 1, "result": {}})))
    assert _request(replayed)["method"] == "thread/start"


def test_codex_app_server_declines_approvals_instead_of_hanging():
    """没有人工确认入口：审批一律自动拒绝，并把这件事告诉用户。"""
    _, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {
                    "id": 7,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "rm -rf /"},
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["log"]
    assert json.loads(run.take_outbound()[0]) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"decision": "decline"},
    }


def test_codex_app_server_reports_failed_turn_and_bad_response():
    _, run = _app_server_run()
    events = list(
        run.feed(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {"id": "t1", "status": "failed", "error": {"message": "模型崩了"}}
                    },
                }
            )
        )
    )
    assert [e.type.value for e in events] == ["error"]
    assert events[0].text == "模型崩了"
    assert run.done is True

    _, broken = _app_server_run()
    events = list(broken.feed(json.dumps({"id": 1, "error": {"message": "不认识这个客户端"}})))
    assert [e.type.value for e in events] == ["error"]
    assert broken.done is True


def test_codex_app_server_ignores_dirty_lines():
    """合法 JSON 但不是对象（数组 / 数字 / 字符串）也不能把整轮运行带崩。"""
    for line in ("[1, 2, 3]", "42", '"oops"'):
        _, run = _app_server_run()
        events = list(run.feed(line))
        assert [e.type.value for e in events] == ["log"], line


def test_hermes_agent_invocation_uses_stream_json_and_oneshot():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(system_prompt=None))
    assert invocation.argv[0] == "hermes"
    assert "chat" in invocation.argv
    assert "--oneshot" in invocation.argv
    assert "--format" in invocation.argv
    fmt_idx = invocation.argv.index("--format")
    assert invocation.argv[fmt_idx + 1] == "stream-json"
    assert "-q" in invocation.argv
    q_idx = invocation.argv.index("-q")
    assert invocation.argv[q_idx + 1] == "hello world"


def test_hermes_agent_session_resume():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(session_id="hermes-sess-1"))
    assert "--session" in invocation.argv
    s_idx = invocation.argv.index("--session")
    assert invocation.argv[s_idx + 1] == "hermes-sess-1"


def test_hermes_agent_system_prompt_prepend():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx())
    q_idx = invocation.argv.index("-q")
    prompt = invocation.argv[q_idx + 1]
    assert prompt.startswith("You are a tester.")


def test_hermes_agent_safe_mode_flag():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(options={"approval_mode": "safe"}))
    assert "--safe-mode" in invocation.argv


def test_hermes_agent_home_dir_env():
    definition = get_agent("hermes-agent")
    invocation = definition.build_invocation(ctx(home_dir="/tmp/hermes-home"))
    assert invocation.env["HERMES_HOME"] == "/tmp/hermes-home"


def test_hermes_agent_stream_parsing():
    definition = get_agent("hermes-agent")
    run = definition.create_run(ctx())
    lines = [
        {"type": "system", "subtype": "init", "session_id": "hs1", "model": "claude-sonnet"},
        {"type": "text", "text": "Hello "},
        {"type": "text", "text": "world"},
        {"type": "tool_use", "name": "read_file", "id": "t1", "input": {"path": "/tmp/x"}},
        {"type": "tool_result", "id": "t1", "output": "file content"},
        {"type": "result", "session_id": "hs1", "exit_code": 0, "tokens": {"input": 10, "output": 5, "total": 15}},
    ]
    events = [e for line in lines for e in run.feed(json.dumps(line))]
    kinds = [e.type.value for e in events]
    assert kinds.count("session") == 1
    assert run.emitted_text == "Hello world"
    assert "tool.call" in kinds and "tool.result" in kinds and "usage" in kinds
    assert run.session_id == "hs1"
