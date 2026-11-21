#!/usr/bin/env python3
"""图片生成 MCP 服务器（stdio 传输）。

一个随平台分发的「平台通用」MCP 服务器：把 OpenAI 兼容网关的生图接口
包成一个 ``generate_image`` 工具，分配到它的智能体就能在对话里生图。

* 不传参考图 → ``POST /v1/images/generations``（文生图）
* 传 ``image`` / ``images`` → ``POST /v1/images/edits``（图生图 / 图片编辑）；
  参考图可以是工作目录内的相对路径（读成 base64 data URL）或 http(s) 地址

配置全部来自环境变量（在「MCP」页新建 / 编辑服务器时填写；值经平台
加密落库，运行时由内核注入本进程，绝不会出现在日志或对话里）：

* ``IMAGE_GEN_GATEWAY``   网关地址（如 ``https://api.example.com`` 或 ``.../v1``）
* ``IMAGE_GEN_MODEL``     生图模型名
* ``IMAGE_GEN_API_KEY``   网关密钥（无鉴权的本地网关可不填）
* ``IMAGE_GEN_TIMEOUT``   单次生图超时秒数（默认 300）

生成的图片落在本进程工作目录（即对话工作目录）的 ``generated-images/``
下，工具结果回给模型的是**工作目录内的相对路径**（如
``generated-images/image-….png``）—— 大图片不进上下文；相对路径既能被内核
按工作目录读取，又能被模型原样写进 ``![](…)`` 在页面上显示成图。结果里还会
带上这个写法提示，免得模型只甩一串地址、图反而看不见。

单文件内的职责分层（自上而下，单向依赖）：

    协议层（JSON-RPC 收发与分发）
      → 工具层（generate_image 的入参校验与编排）
          → 客户端层（调网关 / 下载图片）
              → 配置层（读环境变量、拼 URL）

只用标准库：stdio 传输里 stdout 是协议端口，日志一律走 stderr。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Sequence

SERVER_NAME = "coda-image-gen"
SERVER_VERSION = "1.0.0"
#: MCP 协议版本；initialize 时优先回显客户端请求的版本
PROTOCOL_VERSION = "2024-11-05"

TOOL_NAME = "generate_image"

ENV_GATEWAY = "IMAGE_GEN_GATEWAY"
ENV_MODEL = "IMAGE_GEN_MODEL"
ENV_API_KEY = "IMAGE_GEN_API_KEY"
ENV_TIMEOUT = "IMAGE_GEN_TIMEOUT"
#: 可选：图片的对外访问前缀（如静态图床 ``https://img.example.com``）。
#: 设置后工具结果给出的是绝对 URL，不再依赖「调用方工作目录」——适合经 MCPHub
#: 等网关代跑、与客户端不在同一文件系统的场景。不设置则保持相对路径。
ENV_PUBLIC_BASE_URL = "IMAGE_PUBLIC_BASE_URL"
#: 可选：额外的只读输入根目录（冒号分隔），用于「网关代跑」时读取调用方留在
#: 工作区里的参考图。客户端传 ``attachments/x.png`` 这类相对路径时，除当前工作
#: 目录外，还会到这些根目录的下一层（对话/协作工作区）里按同样的相对路径查找。
ENV_INPUT_ROOTS = "IMAGE_GEN_INPUT_ROOTS"
#: 可选：生成图片在本地保留多少分钟后自动清理（0 / 未设置＝不清理）。
#: 经网关代跑时，图片只是给客户端取回的**中转**：客户端拿到副本后，这份就可以删。
ENV_RETENTION_MINUTES = "IMAGE_RETENTION_MINUTES"

#: 单张图片的字节上限与张数上限（防失控响应把磁盘塞满 / 上下文撑爆）
DEFAULT_TIMEOUT = 300.0
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGES = 4
#: 图生图的参考图张数与单张字节上限（参考图要 base64 进请求体，别让请求撑爆）
MAX_INPUT_IMAGES = 10
MAX_INPUT_BYTES = 10 * 1024 * 1024
_SIZE_RE = re.compile(r"^\d{2,5}[xX]\d{2,5}$")

#: 图片保存的子目录（相对本进程工作目录，即对话工作目录）
IMAGE_SUBDIR = "generated-images"


# --- 配置层 -------------------------------------------------------------------


class ConfigError(RuntimeError):
    """配置缺失：消息面向用户，说明缺什么、去哪里补。"""


def load_config() -> dict[str, Any]:
    """读取环境变量里的网关配置；缺 gateway / model 不在这里报错，
    留到真正调用时才提示 —— 服务器本身要先能启动，错误才能在对话里看到。"""
    timeout = DEFAULT_TIMEOUT
    raw_timeout = (os.environ.get(ENV_TIMEOUT) or "").strip()
    if raw_timeout:
        try:
            timeout = max(1.0, float(raw_timeout))
        except ValueError:
            print(f"{ENV_TIMEOUT} 配置不成秒数（{raw_timeout}），按默认 {DEFAULT_TIMEOUT:.0f}s 处理",
                  file=sys.stderr)
    return {
        "gateway": (os.environ.get(ENV_GATEWAY) or "").strip(),
        "model": (os.environ.get(ENV_MODEL) or "").strip(),
        "api_key": (os.environ.get(ENV_API_KEY) or "").strip(),
        "timeout": timeout,
    }


def _api_base(gateway: str) -> str:
    """把网关地址归一化成 OpenAI 兼容的 ``/v1`` 根。

    用户可能填 ``https://api.example.com`` 也可能填 ``https://api.example.com/v1``，
    两种都收敛到 ``.../v1``（与网关探测的 URL 归一化同一套口径）。
    """
    base = (gateway or "").rstrip("/")
    if not base:
        raise ConfigError(
            f"还没有配置生图网关：请在「MCP」页编辑服务器「图片生成」，"
            f"在参数「网关地址」里填写（对应环境变量 {ENV_GATEWAY}，例如 https://api.example.com）"
        )
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def generations_url(gateway: str) -> str:
    """文生图端点：``POST /v1/images/generations``。"""
    return f"{_api_base(gateway)}/images/generations"


def edits_url(gateway: str) -> str:
    """图生图（图片编辑）端点：``POST /v1/images/edits``。

    参考图在这个端点上传递；往 ``/images/generations`` 塞 ``image`` 会被网关拒绝。
    """
    return f"{_api_base(gateway)}/images/edits"


# --- 客户端层 -----------------------------------------------------------------


class GatewayError(RuntimeError):
    """调用网关失败：消息面向用户，可展示。"""


def _post_json(
    url: str, payload: dict[str, Any], *, api_key: str, timeout: float
) -> Any:
    """POST 一个 JSON 请求并解析响应；错误转成中文的 GatewayError。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = (exc.read() or b"").decode("utf-8", "replace")
        except OSError:  # 读取错误正文失败时退回无正文
            detail = ""
        detail = detail.strip().replace("\n", " ")[:300]
        raise GatewayError(f"生图网关返回 HTTP {exc.code}：{detail or '无响应正文'}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GatewayError(f"无法连接生图网关：{exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise GatewayError("生图网关返回的不是 JSON，无法解析生图结果") from exc


def _download(url: str, *, timeout: float) -> tuple[bytes, str]:
    """下载网关回的图片 URL；带字节上限，错误转成中文的 GatewayError。"""
    request = urllib.request.Request(url, headers={"Accept": "image/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
            mime = (response.headers.get("Content-Type") or "").split(";")[0].strip()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GatewayError(f"下载生成的图片失败：{exc}") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise GatewayError(f"生成的图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB，已放弃下载")
    if not data:
        raise GatewayError("图片地址返回了空内容")
    return data, mime


def _image_items(payload: Any) -> list[dict[str, Any]]:
    """从生图响应里取出图片条目；兼容 OpenAI 的 ``data`` 与个别网关的 ``images``。"""
    if not isinstance(payload, dict):
        return []
    for key in ("data", "images"):
        rows = payload.get(key)
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, dict)]
    return []


#: 按魔数识别图片类型（b64 响应不带 mimeType，扩展名不能瞎猜）
_MAGIC_TYPES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

_EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _known_image_type(data: bytes) -> str | None:
    """按文件头识别图片类型；认不出来返回 None。"""
    for magic, mime in _MAGIC_TYPES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_image_type(data: bytes) -> str:
    """按文件头识别图片类型；认不出来时按 png 兜底（生图网关几乎只回这两种）。"""
    return _known_image_type(data) or "image/png"


#: 网关接受的参考图类型（GIF 等其它格式会在上游被拒，这里提前说清楚）
INPUT_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})

_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _input_roots() -> list[Path]:
    """``IMAGE_GEN_INPUT_ROOTS`` 里配置的只读根目录（不存在/不是目录的忽略）。"""
    raw = (os.environ.get(ENV_INPUT_ROOTS) or "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for part in raw.replace(";", ":").split(":"):
        part = part.strip()
        if not part:
            continue
        root = Path(part).expanduser()
        if root.is_dir():
            roots.append(root.resolve())
    return roots


def _in_roots(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _resolve_input_file(spec: str, roots: Sequence[Path]) -> Path:
    """把参考图入参解析成可读的文件路径。

    * 相对路径：先按当前工作目录解析（本地直连的原有行为）；找不到时，在
      ``IMAGE_GEN_INPUT_ROOTS`` 的各工作区里按「工作区 + 同一相对路径」查找；
    * 绝对路径：必须落在当前工作目录或已配置的根目录内。

    只在网关代跑、客户端与工具不在同一文件系统时才会走到工作区查找那一步。
    """
    cwd = Path.cwd().resolve()
    candidate = Path(spec)
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            raise ValueError(f"参考图路径无法解析：「{spec}」") from None
        if resolved.is_file() and (resolved.is_relative_to(cwd) or _in_roots(resolved, roots)):
            return resolved
        raise ValueError(f"参考图只能读工作目录内的文件：「{spec}」不在工作目录里")

    local = (cwd / candidate).resolve()
    if local.is_file() and local.is_relative_to(cwd):
        return local

    relative = candidate.as_posix().lstrip("./")
    matches: list[Path] = []
    for root in roots:
        # 先按「工作区/相对路径」找（对话与协作工作区都只有一层），再退回全树搜索
        for pattern in (f"*/{relative}", f"**/{relative}"):
            found = [p.resolve() for p in root.glob(pattern) if p.is_file()]
            if found:
                matches.extend(found)
                break
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    if not unique:
        raise ValueError(
            f"参考图不存在：「{spec}」——工作目录和已挂载的工作区里都没找到"
        )
    raise ValueError(
        f"参考图「{spec}」在多个工作区里重名（{len(unique)} 处），请改传绝对路径"
    )


def _read_workspace_image(spec: str) -> tuple[bytes, str]:
    """读取工作目录（或已挂载工作区）内的参考图，返回 (字节, 类型)。"""
    roots = _input_roots()
    resolved = _resolve_input_file(spec, roots)
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ValueError(f"读不到参考图「{spec}」：{exc}") from exc
    if size > MAX_INPUT_BYTES:
        raise ValueError(
            f"参考图「{spec}」超过 {MAX_INPUT_BYTES // (1024 * 1024)} MB，请先压缩或缩小尺寸"
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValueError(f"读不到参考图「{spec}」：{exc}") from exc
    if not data:
        raise ValueError(f"参考图「{spec}」是空文件")
    mime = _known_image_type(data)
    if mime not in INPUT_IMAGE_TYPES:
        raise ValueError(f"参考图「{spec}」的格式不支持，仅支持 PNG / JPEG / WebP")
    return data, mime


def input_image_url(spec: str) -> str:
    """把工具入参里的参考图变成网关认识的 ``image_url``。

    工作目录内的文件读成 base64 data URL —— 网关在另一台机器上，拿不到本机路径；
    http(s) 地址原样传，交给网关自己下载。
    """
    if _HTTP_URL_RE.match(spec):
        return spec
    data, mime = _read_workspace_image(spec)
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def retention_minutes() -> float:
    """``IMAGE_RETENTION_MINUTES`` 解析成分钟数；非法值按 0（不清理）处理。"""
    raw = (os.environ.get(ENV_RETENTION_MINUTES) or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        print(f"{ENV_RETENTION_MINUTES} 配置不成分钟数（{raw}），按不清理处理", file=sys.stderr)
        return 0.0


def prune_expired_images() -> int:
    """删掉超过保留期的生成图片，返回删除数量。

    只动 ``generated-images/`` 下由本服务器命名的 ``image-*`` 文件，不碰调用方的
    其它文件；保留期为 0 时什么都不做。客户端应在保留期内把图片取走。
    """
    minutes = retention_minutes()
    if minutes <= 0:
        return 0
    directory = Path.cwd() / IMAGE_SUBDIR
    if not directory.is_dir():
        return 0
    cutoff = time.time() - minutes * 60
    removed = 0
    for path in directory.iterdir():
        if not path.name.startswith("image-") or path.suffix.lower() not in _EXT_BY_TYPE.values():
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:  # 并发删除/权限问题都不该影响生图
            continue
    return removed


def _save_image(data: bytes) -> tuple[Path, str]:
    """把图片字节写进工作目录的 generated-images/，返回 (路径, 类型)。"""
    prune_expired_images()
    mime = sniff_image_type(data)
    directory = Path.cwd() / IMAGE_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"image-{stamp}-{uuid.uuid4().hex[:6]}{_EXT_BY_TYPE.get(mime, '.png')}"
    path.write_bytes(data)
    return path, mime


def workspace_relative(path: Path) -> str:
    """把工作目录里的绝对路径收敛成**工作目录内的相对路径**。

    工具结果里的路径是给模型抄进回复的：相对路径既能被内核按工作目录读取，又能被前端
    直接渲染成图片（`![](generated-images/…)`）。给绝对路径的话，前端一句 `![](/vol1/…)`
    就解析不到那个只读出口，页面只剩一串路径或一张裂图。真在目录外（理论到不了）才退回绝对路径。
    """
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()


def public_base_url() -> str:
    """静态图床前缀；未配置时返回空串。"""
    return (os.environ.get(ENV_PUBLIC_BASE_URL) or "").strip().rstrip("/")


def image_reference(path: Path) -> str:
    """给模型抄进回复的图片引用。

    * 配了 ``IMAGE_PUBLIC_BASE_URL``：回绝对 URL（图片由图床托管，跨进程/跨容器可访问）；
    * 没配：回工作目录内的相对路径（本地生图、前端按对话工作目录解析）。
    """
    base = public_base_url()
    if base:
        return f"{base}/{path.name}"
    return workspace_relative(path)


# --- 工具层 -------------------------------------------------------------------


TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "要生成的图片内容描述；编辑图片时写清要改成什么样（必填）",
        },
        "image": {
            "type": "string",
            "description": (
                "要编辑的参考图：工作目录内的相对路径（如 attachments/a.png、"
                "generated-images/b.png）或 http(s) 图片地址。提供后走图片编辑（图生图），"
                "不提供就是文生图"
            ),
        },
        "images": {
            "type": "array",
            "items": {"type": "string"},
            "description": f"多张参考图（最多 {MAX_INPUT_IMAGES} 张），与 image 可同时使用；同样支持工作目录相对路径或 http(s) 地址",
        },
        "size": {
            "type": "string",
            "description": "图片尺寸，格式为 宽x高（如 1024x1024）；不填由模型决定",
        },
        "n": {
            "type": "integer",
            "description": "一次生成几张（1-4，默认 1）",
        },
    },
    "required": ["prompt"],
}

TOOL_DESCRIPTION = (
    "生成或编辑图片：按文字描述调用生图网关，生成后返回图片地址"
    "（本地直接运行时是工作目录内的相对路径，经网关/图床托管时是绝对 URL）。"
    "传 image / images（工作目录内的图片路径或 http 地址）"
    "就是图片编辑（图生图），不传就是文生图。需要配图、画示意图、改图时使用。"
)


def _input_images(arguments: dict[str, Any]) -> list[str]:
    """取出 image / images 两个参数合并后的参考图列表；形状不对时给可操作的中文提示。"""
    specs: list[str] = []
    single = arguments.get("image")
    if isinstance(single, str):
        if single.strip():
            specs.append(single.strip())
    elif single is not None:
        raise ValueError("image 参数必须是图片路径或 http(s) 地址的字符串")
    many = arguments.get("images")
    if many is not None:
        if not isinstance(many, list):
            raise ValueError("images 参数必须是图片路径 / 地址的字符串数组")
        for item in many:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("images 参数里的每一项都必须是非空的图片路径或 http(s) 地址")
            specs.append(item.strip())
    if len(specs) > MAX_INPUT_IMAGES:
        raise ValueError(f"参考图最多 {MAX_INPUT_IMAGES} 张，收到 {len(specs)} 张")
    return specs


def generate_image(arguments: dict[str, Any], config: dict[str, Any]) -> str:
    """执行一次生图 / 图片编辑：校验入参 → 调网关 → 落盘 → 返回给模型的说明文本。"""
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("请提供要生成的图片描述（参数 prompt）")
    config = config or load_config()
    if not config["model"]:
        raise ConfigError(
            f"还没有配置生图模型：请在「MCP」页编辑服务器「图片生成」，"
            f"在参数「模型名称」里填写（对应环境变量 {ENV_MODEL}）"
        )
    size = str(arguments.get("size") or "").strip()
    if size and not _SIZE_RE.match(size):
        raise ValueError(f"size 参数的格式应为 宽x高（如 1024x1024），收到的是「{size}」")
    try:
        count = int(arguments.get("n", 1))
    except (TypeError, ValueError):
        raise ValueError("n 参数必须是 1-4 的整数") from None
    if not 1 <= count <= MAX_IMAGES:
        raise ValueError(f"n 参数必须是 1-{MAX_IMAGES} 的整数")
    specs = _input_images(arguments)

    payload: dict[str, Any] = {"model": config["model"], "prompt": prompt, "n": count}
    if size:
        payload["size"] = size.lower()
    if specs:
        # 图生图：参考图只能走 /images/edits；本地文件转成 data URL，网关才拿得到
        payload["images"] = [{"image_url": input_image_url(spec)} for spec in specs]
        target = edits_url(config["gateway"])
    else:
        target = generations_url(config["gateway"])
    result = _post_json(
        target,
        payload,
        api_key=config["api_key"],
        timeout=config["timeout"],
    )

    saved: list[tuple[Path, str]] = []
    failures: list[str] = []
    for item in _image_items(result):
        try:
            b64 = str(item.get("b64_json") or "")
            if b64:
                saved.append(_save_image(base64.b64decode(b64)))
                continue
            url = str(item.get("url") or "")
            if url:
                data, _mime = _download(url, timeout=config["timeout"])
                saved.append(_save_image(data))
                continue
            failures.append("响应里既没有 b64_json 也没有 url")
        except (GatewayError, ValueError) as exc:  # 单张失败不拖垮整批
            failures.append(str(exc))
    if not saved:
        raise GatewayError(
            "生图网关没有返回可用的图片" + (f"（{failures[0]}）" if failures else "")
        )

    if public_base_url():
        where = "可访问地址"
    else:
        where = f"工作目录的 {IMAGE_SUBDIR}/ 下（路径相对工作目录）"
    if specs:
        headline = f"已根据 {len(specs)} 张参考图生成 {len(saved)} 张图片，{where}："
    else:
        headline = f"已生成 {len(saved)} 张图片，{where}："
    lines = [headline]
    for path, mime in saved:
        lines.append(f"- {image_reference(path)}（{mime.removeprefix('image/').upper()}）")
    lines.append(
        "要在回复里直接把图展示出来，就原样写一行 Markdown 图片："
        f"`![]({image_reference(saved[0][0])})` —— 页面会渲染成图；只贴地址的话只会显示成一串字。"
    )
    lines.append("也可以继续对这些文件做后续处理。")
    if failures:
        lines.append(f"有 {len(failures)} 张生成失败：{'；'.join(failures)}")
    return "\n".join(lines)


# --- 协议层 -------------------------------------------------------------------


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_response(msg_id: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
    """工具调用的标准回应：文本内容 + isError 标记（不抛协议错误，让模型看到原因）。"""
    content = [{"type": "text", "text": text}]
    result: dict[str, Any] = {"content": content}
    if is_error:
        result["isError"] = True
    return _result(msg_id, result)


def handle_message(message: Any) -> dict[str, Any] | None:
    """分发一条 JSON-RPC 消息；通知（无 id）静默忽略，返回 None 表示不回应。"""
    if not isinstance(message, dict):
        return _error(None, -32600, "请求必须是 JSON 对象")
    method = str(message.get("method") or "")
    msg_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        return _result(
            msg_id,
            {
                "protocolVersion": str(params.get("protocolVersion") or PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if msg_id is None:
        # notifications/initialized、notifications/cancelled 等通知：无需回应
        return None
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(
            msg_id,
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": TOOL_SCHEMA,
                    }
                ]
            },
        )
    if method == "tools/call":
        name = str(params.get("name") or "")
        if name != TOOL_NAME:
            return _error(msg_id, -32602, f"未知工具：{name}")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            text = generate_image(arguments, load_config())
        except (ConfigError, GatewayError, ValueError) as exc:
            return _tool_response(msg_id, f"生成图片失败：{exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001 - 兜底：不让未知异常拖垮整个服务器
            print(f"生图工具未预期的异常：{exc!r}", file=sys.stderr)
            return _tool_response(msg_id, f"生成图片时发生未预期的错误：{exc}", is_error=True)
        return _tool_response(msg_id, text)
    return _error(msg_id, -32601, f"未知方法：{method}")


def serve() -> int:
    """主循环：逐行读 stdin（每行一条 JSON-RPC 消息），回应写回 stdout。"""
    prune_expired_images()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = _error(None, -32700, "收到无法解析的 JSON 行")
        else:
            try:
                reply = handle_message(message)
            except Exception as exc:  # noqa: BLE001 - 协议层兜底，同上
                print(f"处理消息时未预期的异常：{exc!r}", file=sys.stderr)
                reply = _error(None, -32603, f"内部错误：{exc}")
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve())
