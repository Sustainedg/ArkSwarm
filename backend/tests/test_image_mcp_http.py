"""图片 MCP 的 HTTP 传输层纯函数：文件名白名单、鉴权头与工具入参。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SUPPORT = Path(__file__).resolve().parent.parent.parent / "docker" / "image-mcp" / "http_support.py"


@pytest.fixture(scope="module")
def support():
    spec = importlib.util.spec_from_file_location("image_mcp_http_support", SUPPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    ["image-20261117-120000-abc123.png", "image-x.webp", "image-a.jpg", "image-b.jpeg", "image-c.gif"],
)
def test_safe_image_names(support, name):
    assert support.is_safe_image_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../secret.png",
        "a/b.png",
        "image-.png",
        ".image-x.png",
        "image-x.svg",
        "image-x",
        "attachments/a.png",
        "image-x.png.bak",
    ],
)
def test_unsafe_image_names_rejected(support, name):
    assert not support.is_safe_image_name(name)


def test_content_type_falls_back_to_png(support):
    assert support.content_type_for("image-a.png") == "image/png"
    assert support.content_type_for("image-a.jpg") == "image/jpeg"
    assert support.content_type_for("image-a.unknown") == "image/png"


def test_image_url_strips_trailing_slash(support):
    assert support.image_url("http://h:3070/images/", "image-a.png") == "http://h:3070/images/image-a.png"
    assert support.image_url("http://h:3070/images", "image-a.png") == "http://h:3070/images/image-a.png"


def test_bearer_matches_accepts_both_headers(support):
    token = "s3cret-token"
    assert support.bearer_matches({"authorization": f"Bearer {token}"}, token)
    assert support.bearer_matches({"Authorization": f"bearer {token}"}, token)
    assert support.bearer_matches({"x-api-key": token}, token)
    assert not support.bearer_matches({}, token)
    assert not support.bearer_matches({"authorization": "Bearer wrong"}, token)
    # token 为空表示不鉴权
    assert support.bearer_matches({}, "")


def test_parse_allowed_hosts(support):
    assert support.parse_allowed_hosts(" a:1, b:2 ,,") == ["a:1", "b:2"]
    assert support.parse_allowed_hosts("") == []


def test_tool_arguments_drops_blank_inputs(support):
    arguments = support.tool_arguments("画只猫", "  ", [], "  ", 2)
    assert arguments == {"prompt": "画只猫", "n": 2}

    arguments = support.tool_arguments(
        "改图", "attachments/a.png", [" ", "generated-images/b.png"], "1024x1024", 1
    )
    assert arguments == {
        "prompt": "改图",
        "n": 1,
        "image": "attachments/a.png",
        "images": ["generated-images/b.png"],
        "size": "1024x1024",
    }
