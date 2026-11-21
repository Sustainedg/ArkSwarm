"""把智能体回复里的远程图片取回对话工作目录。

MCP 网关（如 MCPHub 的图床）生成的图片，链接只适合当传输格式：对话历史要自包含，
换机器、清缓存、网关下线都不该让老对话变成裂图。所以助手消息落库前，把正文/工具结果
里匹配配置前缀的图片下载到 ``<工作目录>/generated-images/``，并把 URL 改写为相对路径
——前端本来就会把工作目录里的相对路径解析成对话文件出口。

只处理显式配置的前缀（``CODA_IMAGE_INGEST_PREFIXES``），不会去抓任意外链。
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

#: 单张上限与单条消息的张数上限：防止一次回复把磁盘写满或拖慢落库。
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 8
TIMEOUT_SECONDS = 30.0

#: 图片落到工作目录的哪个子目录（与 image-gen 的约定一致）。
IMAGE_DIR = "generated-images"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
#: URL 尾巴只收保守的 ASCII 字符集：中文标点、括号、引号都会提前截断，
#: 免得把「http://…/x.png（PNG）」里的「（PNG）」一起吞进文件名。
_URL_TAIL = r"[A-Za-z0-9\-._~:/?#@!$&*+;=%]+"
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
)


def _safe_name(url: str, data: bytes) -> str:
    """按 URL 基名 + 内容哈希生成落盘文件名（内容寻址，防同名覆盖）。

    不同 URL 撞基名、或网关同名重生成出不同内容的图时，哈希保证各写各的文件、
    各指向各的副本 —— 不会出现「后写的覆盖先写的」「尺寸碰巧相同就保留过期图」。
    认不出图片扩展名时按魔数推断，再不行用 ``.png`` 兜底。
    """
    name = _UNSAFE_NAME.sub("_", Path(urlparse(url).path).name).strip("._")
    stem = Path(name).stem or "image"
    suffix = Path(name).suffix.lower() if Path(name).suffix.lower() in IMAGE_SUFFIXES else ""
    if not suffix:
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            suffix = dict(_MAGIC).get(next((m for m, _ in _MAGIC if data.startswith(m)), b""), ".png")
    stem = stem[:64]
    digest = hashlib.sha256(data).hexdigest()[:12]
    return f"{stem}-{digest}{suffix}"


def _looks_like_image(data: bytes) -> bool:
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    return any(data.startswith(magic) for magic, _ in _MAGIC)


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def _rewrite(value: Any, mapping: dict[str, str]) -> Any:
    """把嵌套结构里出现的 URL 原样替换成相对路径（结构保持不变）。"""
    if isinstance(value, str):
        for url, relative in mapping.items():
            value = value.replace(url, relative)
        return value
    if isinstance(value, dict):
        return {key: _rewrite(item, mapping) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rewrite(item, mapping) for item in value]
    return value


def _collect_urls(texts: Iterable[str], prefixes: Iterable[str]) -> list[str]:
    found: list[str] = []
    patterns = [
        re.compile(re.escape(prefix.rstrip("/")) + "/" + _URL_TAIL)
        for prefix in prefixes
        if prefix.strip()
    ]
    for text in texts:
        for pattern in patterns:
            for match in pattern.finditer(text):
                url = match.group(0).rstrip(".,;:")
                if url not in found:
                    found.append(url)
    return found


async def _download(client: httpx.AsyncClient, url: str) -> bytes:
    """流式下载并累积字节，超限即刻中止 —— 不会把超大响应整个读进内存。"""
    chunks: list[bytes] = []
    received = 0
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > MAX_IMAGE_BYTES:
                raise ValueError(f"超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
            chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise ValueError("内容为空")
    if not _looks_like_image(data):
        raise ValueError("返回内容不是图片")
    return data


async def ingest_images(
    content: str,
    parts: list[dict[str, Any]],
    workspace: str | Path,
    prefixes: Iterable[str],
) -> tuple[str, list[dict[str, Any]], int]:
    """下载回复里匹配前缀的图片并改写引用。

    返回 ``(改写后的正文, 改写后的 parts, 实际取回张数)``；任何一张失败都只记日志、
    保留原 URL，不影响这一轮对话落库。
    """
    prefixes = [p.strip() for p in prefixes if p and p.strip()]
    if not prefixes:
        return content, parts, 0

    urls = _collect_urls([content, *_iter_strings(parts)], prefixes)
    if not urls:
        return content, parts, 0
    if len(urls) > MAX_IMAGES_PER_MESSAGE:
        logger.warning(
            "回复里匹配到 %d 张生成图片，超过单条上限 %d，只取回前 %d 张，其余保留原链接",
            len(urls), MAX_IMAGES_PER_MESSAGE, MAX_IMAGES_PER_MESSAGE,
        )
        urls = urls[:MAX_IMAGES_PER_MESSAGE]

    target_dir = Path(workspace).resolve() / IMAGE_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("生成图片目录不可用，保留原链接：%s", exc)
        return content, parts, 0

    mapping: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        for url in urls:
            try:
                data = await _download(client, url)
            except Exception as exc:  # noqa: BLE001 - 单张失败不该影响整轮
                logger.warning("取回生成图片失败 %s：%s", url, exc)
                continue
            name = _safe_name(url, data)
            path = target_dir / name
            try:
                # 同名即同内容（文件名带内容哈希），存在就跳过写入，幂等且不覆盖
                if not path.exists():
                    path.write_bytes(data)
            except OSError as exc:
                logger.warning("写生成图片失败 %s：%s", path, exc)
                continue
            mapping[url] = f"{IMAGE_DIR}/{name}"

    if not mapping:
        return content, parts, 0
    return _rewrite(content, mapping), _rewrite(parts, mapping), len(mapping)
