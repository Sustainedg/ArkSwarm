"""图片生成 MCP 服务器：URL 归一化、图片识别、协议分发与端到端生图。

这个服务器是「平台通用 MCP 服务器」的一个随平台分发的实例（stdio 传输），
配置（网关地址 / 模型 / 密钥）走它的环境变量；这里直接驱动脚本本体验证。
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "mcp" / "image_mcp_server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("image_mcp_server", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def image_mcp():
    return load_module()


# --- 配置与 URL 归一化 ---------------------------------------------------------


def test_generations_url_normalises_gateway_shapes(image_mcp):
    """用户填裸域名或带 /v1 的地址都要收敛到 /v1/images/generations。"""
    normalize = image_mcp.generations_url
    assert normalize("https://api.example.com") == "https://api.example.com/v1/images/generations"
    assert normalize("https://api.example.com/") == "https://api.example.com/v1/images/generations"
    assert normalize("https://api.example.com/v1") == "https://api.example.com/v1/images/generations"
    assert normalize("http://127.0.0.1:8000/v1/") == "http://127.0.0.1:8000/v1/images/generations"


def test_edits_url_normalises_gateway_shapes(image_mcp):
    """图生图走同一个 /v1 根下的 /images/edits。"""
    normalize = image_mcp.edits_url
    assert normalize("https://api.example.com") == "https://api.example.com/v1/images/edits"
    assert normalize("https://api.example.com/v1/") == "https://api.example.com/v1/images/edits"


def test_generations_url_requires_gateway(image_mcp):
    with pytest.raises(image_mcp.ConfigError, match="IMAGE_GEN_GATEWAY"):
        image_mcp.generations_url("")


def test_load_config_reads_env(monkeypatch, image_mcp):
    monkeypatch.setenv(image_mcp.ENV_GATEWAY, "https://gw.example/v1")
    monkeypatch.setenv(image_mcp.ENV_MODEL, "flux-dev")
    monkeypatch.setenv(image_mcp.ENV_API_KEY, "sk-test-not-a-real-key")
    monkeypatch.setenv(image_mcp.ENV_TIMEOUT, "120")
    config = image_mcp.load_config()
    assert config["gateway"] == "https://gw.example/v1"
    assert config["model"] == "flux-dev"
    assert config["api_key"] == "sk-test-not-a-real-key"
    assert config["timeout"] == 120.0


def test_load_config_timeout_falls_back_to_default(monkeypatch, image_mcp):
    monkeypatch.setenv(image_mcp.ENV_TIMEOUT, "abc")
    assert image_mcp.load_config()["timeout"] == image_mcp.DEFAULT_TIMEOUT


# --- 图片类型识别 --------------------------------------------------------------


def test_sniff_image_type_by_magic(image_mcp):
    assert image_mcp.sniff_image_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert image_mcp.sniff_image_type(b"\xff\xd8\xff....") == "image/jpeg"
    assert image_mcp.sniff_image_type(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
    # 认不出来的内容按 png 兜底（生图网关几乎只回 png/jpeg）
    assert image_mcp.sniff_image_type(b"\x00\x01\x02") == "image/png"


# --- 参考图入参（图生图） ------------------------------------------------------


def test_input_image_url_reads_workspace_file(image_mcp, monkeypatch, tmp_path):
    """工作目录内的参考图要转成 base64 data URL：网关在另一台机器上，读不到本机路径。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "attachments").mkdir()
    source = tmp_path / "attachments" / "in.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
    url = image_mcp.input_image_url("attachments/in.png")
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == source.read_bytes()


def test_input_image_url_passes_http_through(image_mcp):
    """http(s) 地址原样交给网关下载，不在本地读文件。"""
    assert image_mcp.input_image_url("https://cdn.example/a.png") == "https://cdn.example/a.png"


def test_input_image_url_rejects_outside_workspace(image_mcp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="工作目录内"):
        image_mcp.input_image_url(str(tmp_path.parent / "outside.png"))


def test_input_image_url_rejects_unsupported_type(image_mcp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in.gif").write_bytes(b"GIF89a-not-supported")
    with pytest.raises(ValueError, match="PNG / JPEG / WebP"):
        image_mcp.input_image_url("in.gif")


# --- 协议分发 ------------------------------------------------------------------


def test_initialize_and_list_tools(image_mcp):
    reply = image_mcp.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
    )
    assert reply["result"]["serverInfo"]["name"] == image_mcp.SERVER_NAME
    # 回显客户端请求的协议版本
    assert reply["result"]["protocolVersion"] == "2025-03-26"
    assert reply["result"]["capabilities"] == {"tools": {}}

    reply = image_mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = reply["result"]["tools"]
    assert [tool["name"] for tool in tools] == [image_mcp.TOOL_NAME]
    assert tools[0]["inputSchema"]["required"] == ["prompt"]
    # 参考图入参是图生图的入口，工具描述里也要说清楚
    assert {"image", "images"} <= set(tools[0]["inputSchema"]["properties"])


def test_notifications_are_silently_ignored(image_mcp):
    assert image_mcp.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_reported(image_mcp):
    reply = image_mcp.handle_message({"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
    assert reply["error"]["code"] == -32601


# --- 生图编排（不碰网络：替换掉网关调用）----------------------------------------


def _b64_png(payload: bytes = b"\x89PNG\r\n\x1a\nfake-image") -> str:
    return base64.b64encode(payload).decode()


def test_generate_image_saves_files_and_returns_paths(
    image_mcp, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        image_mcp,
        "_post_json",
        lambda *args, **kwargs: {
            "data": [
                {"b64_json": _b64_png()},
                {"url": "https://cdn.example/img.png"},
            ]
        },
    )
    monkeypatch.setattr(
        image_mcp, "_download", lambda *args, **kwargs: (b"\x89PNG\r\n\x1a\nsecond", "image/png")
    )
    text = image_mcp.generate_image(
        {"prompt": "一只戴墨镜的猫", "size": "1024x1024", "n": 2},
        {"gateway": "https://gw.example/v1", "model": "flux-dev", "api_key": "", "timeout": 30.0},
    )
    assert "已生成 2 张图片" in text
    files = sorted((tmp_path / "generated-images").glob("image-*"))
    assert len(files) == 2
    assert files[0].read_bytes().startswith(b"\x89PNG")
    # 返回给模型的是工作目录内的相对路径（前端才解析得到、页面才画得出图），不是图片本体
    assert f"generated-images/{files[0].name}" in text
    assert str(tmp_path) not in text
    assert "![](" in text


def test_generate_image_with_reference_uses_edits(image_mcp, monkeypatch, tmp_path):
    """传参考图时改走 /v1/images/edits，并把本地图片转成网关能取的 data URL。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "in.png").write_bytes(b"\x89PNG\r\n\x1a\ninput")
    calls: list[tuple[str, dict]] = []

    def fake_post(url, payload, **kwargs):
        calls.append((url, payload))
        return {"data": [{"b64_json": _b64_png()}]}

    monkeypatch.setattr(image_mcp, "_post_json", fake_post)
    text = image_mcp.generate_image(
        {"prompt": "把背景改成蓝色", "image": "attachments/in.png", "size": "1024x1024"},
        {"gateway": "https://gw.example/v1", "model": "seedream", "api_key": "", "timeout": 30.0},
    )
    url, payload = calls[0]
    assert url == "https://gw.example/v1/images/edits"
    assert payload["images"][0]["image_url"].startswith("data:image/png;base64,")
    assert payload["prompt"] == "把背景改成蓝色"
    assert "已根据 1 张参考图生成 1 张图片" in text


def test_generate_image_merges_image_and_images(image_mcp, monkeypatch, tmp_path):
    """image 与 images 可以一起用；多张参考图按顺序进同一份请求。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.png").write_bytes(b"\x89PNG\r\n\x1a\na")
    (tmp_path / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nb")
    captured: dict = {}

    def fake_post(url, payload, **kwargs):
        captured.update(payload)
        return {"data": [{"b64_json": _b64_png()}]}

    monkeypatch.setattr(image_mcp, "_post_json", fake_post)
    image_mcp.generate_image(
        {"prompt": "合成这两张图", "image": "a.png", "images": ["b.png", "https://cdn.example/c.png"]},
        {"gateway": "https://gw.example/v1", "model": "seedream", "api_key": "", "timeout": 30.0},
    )
    assert [row["image_url"].split(",")[0] for row in captured["images"][:2]] == [
        "data:image/png;base64",
        "data:image/png;base64",
    ]
    assert captured["images"][2]["image_url"] == "https://cdn.example/c.png"


def test_generate_image_validates_reference_images(image_mcp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = {"gateway": "https://gw.example/v1", "model": "seedream", "api_key": "", "timeout": 30.0}
    with pytest.raises(ValueError, match="最多"):
        image_mcp.generate_image({"prompt": "x", "images": ["a.png"] * 11}, config)
    with pytest.raises(ValueError, match="字符串数组"):
        image_mcp.generate_image({"prompt": "x", "images": "a.png"}, config)
    with pytest.raises(ValueError, match="不存在"):
        image_mcp.generate_image({"prompt": "x", "image": "missing.png"}, config)


def test_generate_image_validates_arguments(image_mcp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = {"gateway": "https://gw.example/v1", "model": "flux-dev", "api_key": "", "timeout": 30.0}
    with pytest.raises(ValueError, match="prompt"):
        image_mcp.generate_image({}, config)
    with pytest.raises(ValueError, match="宽x高"):
        image_mcp.generate_image({"prompt": "x", "size": "big"}, config)
    with pytest.raises(ValueError, match="1-4"):
        image_mcp.generate_image({"prompt": "x", "n": 9}, config)


def test_generate_image_requires_model(image_mcp, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(image_mcp.ENV_GATEWAY, "https://gw.example/v1")
    # 模型没配：要在调用时给出可操作的中文提示，而不是启动就崩
    with pytest.raises(image_mcp.ConfigError, match="IMAGE_GEN_MODEL"):
        image_mcp.generate_image({"prompt": "x"}, image_mcp.load_config())


def test_tools_call_reports_failures_as_tool_errors(image_mcp, monkeypatch, tmp_path):
    """网关报错要走 isError 的工具结果（让模型看到原因），而不是协议层 error。"""
    monkeypatch.chdir(tmp_path)

    def boom(*args, **kwargs):
        raise image_mcp.GatewayError("生图网关返回 HTTP 502：上游不可用")

    monkeypatch.setattr(image_mcp, "_post_json", boom)
    monkeypatch.setenv(image_mcp.ENV_GATEWAY, "https://gw.example/v1")
    monkeypatch.setenv(image_mcp.ENV_MODEL, "flux-dev")
    reply = image_mcp.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "generate_image", "arguments": {"prompt": "x"}},
        }
    )
    assert "error" not in reply
    assert reply["result"]["isError"] is True
    assert "HTTP 502" in reply["result"]["content"][0]["text"]


# --- 端到端：真的起服务器进程 + 假生图网关 --------------------------------------


class _FakeGateway(BaseHTTPRequestHandler):
    """假生图网关：校验请求形状后回一张 1×1 PNG 的 b64。"""

    def do_POST(self):  # noqa: N802 - http.server 的命名约定
        assert self.path.endswith(("/v1/images/generations", "/v1/images/edits")), self.path
        is_edit = self.path.endswith("/v1/images/edits")
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != "Bearer sk-test-not-a-real-key":
            self._reject(401, "密钥不对")
        if payload.get("model") != "flux-dev" or not payload.get("prompt"):
            self._reject(400, "请求体不对")
        if is_edit:
            images = payload.get("images")
            if not (
                isinstance(images, list)
                and images
                and str(images[0].get("image_url") or "").startswith("data:image/png;base64,")
            ):
                self._reject(400, "参考图不对")
        body = json.dumps({"data": [{"b64_json": _b64_png()}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, code: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        raise AssertionError(f"假网关拒绝了请求：{code} {message}")

    def log_message(self, *args):  # 让测试输出安静
        pass


def test_stdio_round_trip_against_fake_gateway(image_mcp, monkeypatch, tmp_path):
    """起真实的服务器子进程，走完整的 stdio JSON-RPC：initialize → tools/list → tools/call。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        monkeypatch.chdir(tmp_path)
        import subprocess

        env = {
            "PATH": "/usr/bin:/bin",
            image_mcp.ENV_GATEWAY: f"http://{host}:{port}",
            image_mcp.ENV_MODEL: "flux-dev",
            image_mcp.ENV_API_KEY: "sk-test-not-a-real-key",
        }
        process = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "generate_image",
                                "arguments": {"prompt": "一只戴墨镜的猫", "n": 1},
                            },
                        }
                    ),
                ]
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=tmp_path,
        )
        assert process.returncode == 0, process.stderr
        replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        by_id = {reply.get("id"): reply for reply in replies}
        assert by_id[1]["result"]["serverInfo"]["name"] == image_mcp.SERVER_NAME
        assert by_id[2]["result"]["tools"][0]["name"] == image_mcp.TOOL_NAME
        call = by_id[3]
        assert "error" not in call
        text = call["result"]["content"][0]["text"]
        assert "已生成 1 张图片" in text
        files = list((tmp_path / "generated-images").glob("image-*.png"))
        assert len(files) == 1
        assert f"generated-images/{files[0].name}" in text
        assert str(tmp_path) not in text
        assert files[0].read_bytes().startswith(b"\x89PNG")
    finally:
        server.shutdown()
        server.server_close()


def test_stdio_edit_round_trip_against_fake_gateway(image_mcp, monkeypatch, tmp_path):
    """端到端走图生图：参考图从工作目录读入，工具结果仍是相对路径。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        monkeypatch.chdir(tmp_path)
        (tmp_path / "attachments").mkdir()
        (tmp_path / "attachments" / "in.png").write_bytes(b"\x89PNG\r\n\x1a\ninput")
        import subprocess

        env = {
            "PATH": "/usr/bin:/bin",
            image_mcp.ENV_GATEWAY: f"http://{host}:{port}",
            image_mcp.ENV_MODEL: "flux-dev",
            image_mcp.ENV_API_KEY: "sk-test-not-a-real-key",
        }
        process = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "generate_image",
                                "arguments": {
                                    "prompt": "把方块改成蓝色",
                                    "image": "attachments/in.png",
                                },
                            },
                        }
                    ),
                ]
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            cwd=tmp_path,
        )
        assert process.returncode == 0, process.stderr
        replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        call = {reply.get("id"): reply for reply in replies}[2]
        assert "error" not in call
        text = call["result"]["content"][0]["text"]
        assert "已根据 1 张参考图生成 1 张图片" in text
        assert str(tmp_path) not in text
        assert list((tmp_path / "generated-images").glob("image-*.png"))
    finally:
        server.shutdown()
        server.server_close()


# --- 与平台的衔接：作为「平台通用」服务器建进目录 -------------------------------



async def test_register_as_platform_mcp_server(client):
    """按内核实际会用的形状注册（stdio + 环境变量注入配置），密钥只回掩码。"""
    response = await client.post(
        "/api/mcp-servers",
        json={
            "name": "图片生成",
            "description": "调用生图网关 POST /v1/images/generations 生成图片",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(SCRIPT)],
            "env": {
                "IMAGE_GEN_GATEWAY": "https://gw.example.com",
                "IMAGE_GEN_MODEL": "flux-dev",
                "IMAGE_GEN_API_KEY": "sk-test-not-a-real-key",
            },
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_kind"] == "manual"
    assert body["editable"] is True
    assert body["env_keys"] == ["IMAGE_GEN_API_KEY", "IMAGE_GEN_GATEWAY", "IMAGE_GEN_MODEL"]
    # 明文密钥绝不出现，值只回掩码
    assert "sk-test-not-a-real-key" not in json.dumps(body)
    assert all(body["env_preview"].get(key) for key in body["env_keys"])


# --- 生成图片保留期 -----------------------------------------------------------------


def test_prune_expired_images_removes_only_old_files(image_mcp, monkeypatch, tmp_path):
    """超过保留期才删，且只动本服务器命名的 image-* 文件。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(image_mcp.ENV_RETENTION_MINUTES, "60")
    folder = tmp_path / image_mcp.IMAGE_SUBDIR
    folder.mkdir()
    old = folder / "image-old.png"
    old.write_bytes(b"\x89PNG\r\n\x1a\n")
    fresh = folder / "image-fresh.png"
    fresh.write_bytes(b"\x89PNG\r\n\x1a\n")
    other = folder / "notes.txt"
    other.write_text("别动我")
    os.utime(old, (0, 0))

    assert image_mcp.prune_expired_images() == 1
    assert not old.exists()
    assert fresh.exists() and other.exists()


def test_prune_disabled_without_retention(image_mcp, monkeypatch, tmp_path):
    """默认不清理：本地直连时图片就是最终产物。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(image_mcp.ENV_RETENTION_MINUTES, raising=False)
    folder = tmp_path / image_mcp.IMAGE_SUBDIR
    folder.mkdir()
    old = folder / "image-old.png"
    old.write_bytes(b"\x89PNG\r\n\x1a\n")
    os.utime(old, (0, 0))

    assert image_mcp.prune_expired_images() == 0
    assert old.exists()
