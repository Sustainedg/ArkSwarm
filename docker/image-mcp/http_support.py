"""图片 MCP 的 HTTP 传输层里不依赖 MCP SDK 的纯函数。

单独放在这里是为了让后端测试不必安装 ``mcp`` 依赖：文件名校验、图片 URL 拼接、
鉴权头判断与工具入参归一化都是纯逻辑，可以直接测。
"""
from __future__ import annotations

import hmac
import mimetypes
import re
from collections.abc import Mapping
from pathlib import Path

#: 只暴露本服务自己写出的文件；调用方上传的其它名字一律不认。
IMAGE_NAME_RE = re.compile(r"^image-[A-Za-z0-9][A-Za-z0-9._-]*$")

#: 允许的图片扩展名（与 image_mcp_server.py 的 ``_EXT_BY_TYPE`` 保持一致）。
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})


def is_safe_image_name(name: str) -> bool:
    """判断请求的图片名是否只指向本服务生成的文件。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return False
    if not IMAGE_NAME_RE.match(name):
        return False
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


def content_type_for(name: str) -> str:
    """按扩展名给出图片 Content-Type；认不出来时按 PNG 兜底。"""
    guessed = mimetypes.guess_type(name)[0]
    return guessed if guessed and guessed.startswith("image/") else "image/png"


def image_url(base: str, name: str) -> str:
    """把图片文件名拼成对外 URL；``base`` 末尾的斜杠会被收敛。"""
    return f"{base.rstrip('/')}/{name}"


def bearer_matches(headers: Mapping[str, str], token: str) -> bool:
    """校验 ``Authorization: Bearer`` 或 ``x-api-key``；token 为空时视为不鉴权。"""
    if not token:
        return True
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    if raw.lower().startswith("bearer "):
        supplied = raw[7:].strip()
    else:
        supplied = (
            headers.get("x-api-key")
            or headers.get("X-Api-Key")
            or ""
        )
    return bool(supplied) and hmac.compare_digest(supplied, token)


def parse_allowed_hosts(raw: str) -> list[str]:
    """逗号分隔的 Host 白名单；空值返回空列表。"""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def tool_arguments(
    prompt: str,
    image: str | None = None,
    images: list[str] | None = None,
    size: str | None = None,
    n: int = 1,
) -> dict[str, object]:
    """把 FastMCP 的具名参数还原成生图脚本认识的入参。

    空字符串 / 空列表等同于没传，避免把「空参考图」当成一次图片编辑。
    """
    arguments: dict[str, object] = {"prompt": prompt, "n": n}
    if image and image.strip():
        arguments["image"] = image.strip()
    if images:
        cleaned = [item.strip() for item in images if isinstance(item, str) and item.strip()]
        if cleaned:
            arguments["images"] = cleaned
    if size and size.strip():
        arguments["size"] = size.strip()
    return arguments
