"""系统管理 MCP 服务器：配置归一、ro 拦截、工具编排与端到端调用。

这个服务器是「平台通用 MCP 服务器」的一个随平台分发的实例（stdio 传输），
配置（平台地址 / PAT / 权限模式）走它的环境变量；这里直接驱动脚本本体验证。
端到端用例起一个假的平台 API（``ThreadingHTTPServer``），验证工具真的会带着
PAT 调 REST API，并且 ro 模式把写操作拦在门外。
"""
from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "mcp" / "arkswarm_admin_mcp_server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("arkswarm_admin_mcp_server", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def admin_mcp():
    return load_module()


# --- 配置层 -------------------------------------------------------------------


def test_api_root_normalises_platform_shapes(admin_mcp):
    """用户填裸域名 / 带端口 / 带 /api 的地址都要收敛到 /api 根。"""
    normalize = admin_mcp.api_root
    assert normalize("http://127.0.0.1:8010") == "http://127.0.0.1:8010/api"
    assert normalize("http://127.0.0.1:8010/") == "http://127.0.0.1:8010/api"
    assert normalize("http://127.0.0.1:8010/api") == "http://127.0.0.1:8010/api"
    assert normalize("http://127.0.0.1:8010/api/") == "http://127.0.0.1:8010/api"
    assert normalize("127.0.0.1:8010") == "http://127.0.0.1:8010/api"
    assert normalize("https://ark.example.com") == "https://ark.example.com/api"


def test_load_config_reads_env(monkeypatch, admin_mcp):
    monkeypatch.setenv(admin_mcp.ENV_BASE_URL, "http://host:8010")
    monkeypatch.setenv(admin_mcp.ENV_TOKEN, "pat_" + "a" * 64)
    monkeypatch.setenv(admin_mcp.ENV_MODE, "ro")
    monkeypatch.setenv(admin_mcp.ENV_TIMEOUT, "30")
    config = admin_mcp.load_config()
    assert config["base_url"] == "http://host:8010/api"
    assert config["mode"] == admin_mcp.MODE_RO
    assert config["timeout"] == 30.0


def test_load_config_bad_values_fall_back(monkeypatch, admin_mcp):
    """超时不成秒数、模式写错都按安全默认处理（只告警，不炸服务器）。"""
    monkeypatch.delenv(admin_mcp.ENV_BASE_URL, raising=False)
    monkeypatch.delenv(admin_mcp.ENV_TOKEN, raising=False)
    monkeypatch.setenv(admin_mcp.ENV_MODE, "read-only")
    monkeypatch.setenv(admin_mcp.ENV_TIMEOUT, "abc")
    config = admin_mcp.load_config()
    assert config["base_url"] == ""
    assert config["token"] == ""
    # 内置服务器默认只读；模式写错时更要保守，不能反而放开写权限。
    assert config["mode"] == admin_mcp.MODE_RO
    assert config["timeout"] == admin_mcp.DEFAULT_TIMEOUT


# --- ro 模式与确认拦截 ----------------------------------------------------------


def test_rw_required_in_ro_mode(admin_mcp):
    config = {"base_url": "http://x/api", "token": "t", "mode": admin_mcp.MODE_RO, "timeout": 1.0}
    with pytest.raises(admin_mcp.ApiError, match="只读模式"):
        admin_mcp._require_rw(config)


def test_rw_allowed_in_rw_mode(admin_mcp):
    config = {"mode": admin_mcp.MODE_RW}
    admin_mcp._require_rw(config)  # 不抛错即通过


@pytest.mark.parametrize("bad_confirm", [None, False, "true", 1])
def test_confirm_must_be_explicit_true(admin_mcp, bad_confirm):
    """confirm 必须是布尔 true —— 字符串 "true" / 数字都不算，防模型蒙混过关。"""
    with pytest.raises(admin_mcp.ApiError, match="confirm=true"):
        admin_mcp._need_confirm({"confirm": bad_confirm})


def test_confirm_true_passes(admin_mcp):
    admin_mcp._need_confirm({"confirm": True})  # 不抛错即通过


# --- 协作组节点 / 边的宽松映射 ---------------------------------------------------


def test_coerce_nodes_maps_loose_field_names(admin_mcp):
    """模型给 agent / agent_id 也能映射到 agent_profile_id。"""
    nodes = admin_mcp._coerce_nodes(
        [
            {"key": "ceo", "agent": "agent-1", "duty": "ceo"},
            {"key": "eng", "agent_id": "agent-2"},
        ]
    )
    assert nodes[0]["agent_profile_id"] == "agent-1"
    assert nodes[0]["duty"] == "ceo"
    assert nodes[1]["agent_profile_id"] == "agent-2"


def test_coerce_nodes_rejects_missing_fields(admin_mcp):
    with pytest.raises(admin_mcp.ApiError, match="key"):
        admin_mcp._coerce_nodes([{"duty": "ceo"}])


def test_coerce_edges_rejects_unknown_node(admin_mcp):
    nodes = [{"key": "a", "agent_profile_id": "x"}]
    with pytest.raises(admin_mcp.ApiError, match="不存在的岗位"):
        admin_mcp._coerce_edges([{"from_key": "a", "to_key": "ghost"}], nodes)


# --- 协议层 -------------------------------------------------------------------


def test_tools_list_shape(admin_mcp):
    """tools/list 回全部工具，名字 / 描述 / schema 齐全，写工具都带 confirm 参数。"""
    reply = admin_mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {item["name"]: item for item in reply["result"]["tools"]}
    assert "agent_list" in tools
    assert "team_create" in tools
    assert "skill_delete" in tools
    assert "mcp_create" in tools
    for name in admin_mcp.TOOL_HANDLERS:
        assert name in tools
    for name, tool_spec in admin_mcp.TOOL_HANDLERS.items():
        assert tools[name]["description"]
        assert tools[name]["inputSchema"]["type"] == "object"
        if tool_spec["mutating"]:
            assert "confirm" in tools[name]["inputSchema"].get("properties", {}), name


def test_unknown_tool_rejected(admin_mcp):
    reply = admin_mcp.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert reply["error"]["code"] == -32602


def test_notifications_are_silent(admin_mcp):
    assert admin_mcp.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_initialize_echoes_protocol(admin_mcp):
    reply = admin_mcp.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["serverInfo"]["name"] == "arkswarm-admin"


def test_missing_config_gives_actionable_error(admin_mcp):
    """没配地址 / 令牌时，错误信息要告诉用户去哪里补，而不是抛协议错误。"""
    reply = admin_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "agent_list", "arguments": {}},
        }
    )
    result = reply["result"]
    assert result["isError"] is True
    assert "ARKSWARM_BASE_URL" in result["content"][0]["text"]


# --- 端到端：对着假平台跑 -------------------------------------------------------


class _FakePlatform(BaseHTTPRequestHandler):
    """只够用的假平台：记录请求，回固定 JSON。行为由类属性驱动。"""

    records: list[tuple[str, str, dict]] = []
    responses: dict[str, tuple[int, dict]] = {}

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def _handle(self, method: str) -> None:
        if self.headers.get("Authorization") != "Bearer pat-good-token":
            self._respond(401, {"detail": "个人访问令牌无效或已吊销"})
            return
        body = self._read_body()
        _FakePlatform.records.append((method, self.path, body))
        status, payload = _FakePlatform.responses.get(
            self.path, (200, {"items": [], "detail": "未模拟的路径"})
        )
        if method == "DELETE":
            self.send_response(204)
            self.end_headers()
            return
        self._respond(status, payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server 的约定方法名
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def log_message(self, *args) -> None:  # 测试里别往 stderr 打访问日志
        pass


@pytest.fixture()
def fake_platform(monkeypatch):
    """起一个假平台，并把地址 / PAT / 模式注入环境变量（工具每次调用都走真实的 load_config）。"""
    _FakePlatform.records = []
    _FakePlatform.responses = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakePlatform)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("ARKSWARM_BASE_URL", base_url)
    monkeypatch.setenv("ARKSWARM_TOKEN", "pat-good-token")
    # 端到端用例验证的是工具编排与 API 调用；写操作需要显式开 rw（内置默认是只读）。
    monkeypatch.setenv("ARKSWARM_MODE", "rw")
    yield base_url
    server.shutdown()
    server.server_close()


def test_agent_list_calls_platform_with_pat(fake_platform, admin_mcp):
    """agent_list 真的会带 Bearer PAT 调 /api/agents。"""
    _FakePlatform.responses["/api/agents"] = (
        200,
        {"items": [{"id": "a1", "name": "小助手", "type_id": "claude-code", "enabled": True}]},
    )
    reply = admin_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "agent_list", "arguments": {}},
        }
    )
    assert reply["result"].get("isError") is not True
    assert "小助手" in reply["result"]["content"][0]["text"]
    assert _FakePlatform.records[0][1] == "/api/agents"


def test_401_gives_actionable_hint(fake_platform, monkeypatch, admin_mcp):
    """PAT 被吊销（401）时，提示用户去「设置 → 账号」重新生成。"""
    monkeypatch.setenv(admin_mcp.ENV_TOKEN, "pat-revoked-token")  # 假平台只认 pat-good-token
    reply = admin_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "agent_list", "arguments": {}},
        }
    )
    text = reply["result"]["content"][0]["text"]
    assert reply["result"].get("isError") is True
    assert "401" in text and "设置 → 账号" in text


def test_ro_mode_blocks_writes(fake_platform, monkeypatch, admin_mcp):
    """ro 模式下写操作连平台都不碰，直接拒绝。"""
    monkeypatch.setenv(admin_mcp.ENV_MODE, "ro")
    reply = admin_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "skill_delete", "arguments": {"name_or_id": "x", "confirm": True}},
        }
    )
    assert reply["result"]["isError"] is True
    assert "只读模式" in reply["result"]["content"][0]["text"]
    assert _FakePlatform.records == []  # 根本没发请求


def test_missing_confirm_blocks_write(fake_platform, admin_mcp):
    reply = admin_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "mcp_create", "arguments": {"name": "新服务器"}},
        }
    )
    assert reply["result"]["isError"] is True
    assert "confirm=true" in reply["result"]["content"][0]["text"]


def test_agent_create_assigns_skills_afterwards(fake_platform, admin_mcp):
    """创建后补技能分配：POST /agents 再 PUT /agents/{id}/skills。"""
    _FakePlatform.responses["/api/agents"] = (
        201,
        {"id": "new-1", "name": "管家", "type_id": "claude-code"},
    )
    reply = admin_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "agent_create",
                "arguments": {
                    "name": "管家",
                    "type_id": "claude-code",
                    "skill_ids": ["s1", "s2"],
                    "confirm": True,
                },
            },
        }
    )
    assert reply["result"].get("isError") is not True, reply["result"]["content"][0]["text"]
    requests = [(method, path) for method, path, _ in _FakePlatform.records]
    assert ("POST", "/api/agents") in requests
    assert ("PUT", "/api/agents/new-1/skills") in requests

def test_skill_get_truncates_long_body(fake_platform, admin_mcp):
    """SKILL.md 内容超长要截断，别把上下文灌爆。"""
    _FakePlatform.responses["/api/skills"] = (200, {"items": [{"id": "sk-1", "slug": "demo", "name": "演示"}]})
    _FakePlatform.responses["/api/skills/sk-1"] = (
        200,
        {"name": "演示", "id": "sk-1", "source_kind": "user", "body": "x" * 5000, "agents": []},
    )
    reply = admin_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {"name": "skill_get", "arguments": {"name_or_id": "demo"}},
        }
    )
    text = reply["result"]["content"][0]["text"]
    assert "已截断" in text
    assert len(text) < 4000
