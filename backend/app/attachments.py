"""附件：把上传的内容落进某个工作目录的 ``attachments/``，并把路径写进提示词。

对话（``api/conversations.py``）与协作（``collab/``）都用这一份实现：命名、大小上限、
base64 解码与「📎 附件」提示块只此一处，免得两边各写一套后悄悄漂移。

为什么附件不经过适配器：智能体本来就能读工作目录里的文件，发消息时把**路径**写进提示词即可，
所有内核（Claude Code / Codex / Gemini CLI / custom-cli）都自动支持，不必改事件协议。
"""
from __future__ import annotations

import base64
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

#: 单个附件的上限：手机上多为截图与文档，8 MB 足够，也免得太大的 base64 撑爆请求体。
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


class AttachmentError(ValueError):
    """附件不合法（不是 base64 / 空内容 / 超大 / 跑出工作目录）。

    消息面向用户、可直接展示；``status_code`` 交给 API 层当 HTTP 状态码用。
    """

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class StoredAttachment:
    """一个已经落盘的附件。"""

    name: str
    rel_path: str
    path: str
    size: int

    def to_dict(self) -> dict:
        return {"name": self.name, "rel_path": self.rel_path, "path": self.path, "size": self.size}


def safe_attachment_name(raw: str) -> str:
    """只留文件名本身：去掉目录成分与危险字符，杜绝「../」写到工作目录之外。"""
    name = Path(raw.replace("\\", "/")).name.strip()
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in " ._-()[]（）【】")
    return cleaned.strip(" .")[:120] or "attachment.bin"


def decode_attachment(content_base64: str, limit: int = MAX_ATTACHMENT_BYTES) -> bytes:
    """解开 base64 并做大小/非空校验；``limit`` 由调用方传入，便于测试压小上限。"""
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (ValueError, TypeError):
        raise AttachmentError("附件内容不是合法的 base64") from None
    if not raw:
        raise AttachmentError("附件内容为空")
    if len(raw) > limit:
        raise AttachmentError(
            f"附件过大：单个附件不能超过 {limit // 1024 // 1024} MB", status_code=413
        )
    return raw


def store_attachment(workspace: Path, name: str, raw: bytes) -> StoredAttachment:
    """把附件写进 ``<工作目录>/attachments/``，文件名前缀时间戳 + 随机串避免重名覆盖。"""
    clean = safe_attachment_name(name)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rel_path = f"attachments/{stamp}-{uuid.uuid4().hex[:6]}-{clean}"
    target = Path(workspace) / rel_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    except OSError as exc:
        # 磁盘满 / 目录不可写这类问题要给用户一句能看懂的话，而不是一个 500 堆栈（铁律 11）。
        raise AttachmentError(f"附件保存失败：{target}（{exc}）", status_code=500) from exc
    return StoredAttachment(name=clean, rel_path=rel_path, path=str(target), size=len(raw))


def resolve_workspace_file(relative: str, workspace: Path | str) -> Path:
    """把工作目录里的一个相对路径解析成真实绝对路径，并确保确实在目录内。

    两处都用得着：附件进提示词前要校验（:func:`attachment_relative_path`），
    以及把工作目录里的文件回给浏览器看（图片预览等）。只放行目录**内部**的普通文件 ——
    ``../`` 穿越与目录本身都拒绝，异常信息面向用户、可直接展示。
    """
    try:
        root = Path(workspace).resolve()
        candidate = Path(relative)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise AttachmentError(f"附件不可用（不在工作目录内或已删除）：{relative}")
    except AttachmentError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise AttachmentError(f"附件不可用（路径无法解析）：{relative}") from exc
    return candidate


def attachment_relative_path(relative: str, workspace: Path | str) -> str:
    """校验附件确实在本次运行的工作目录内，返回规范化的相对路径。"""
    root = Path(workspace).resolve()
    return resolve_workspace_file(relative, workspace).relative_to(root).as_posix()


def guess_media_type(name: str) -> str:
    """按文件名猜 MIME，供「把工作目录里的文件回给浏览器」用；认不出就是二进制流。"""
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def attachment_block(rel_paths: Sequence[str]) -> str:
    """提示词里的附件清单；没有附件时返回空串。"""
    if not rel_paths:
        return ""
    lines = "\n".join(f"- {path}" for path in rel_paths)
    return f"📎 附件（已存入工作目录，可直接读取）：\n{lines}"


def compose_with_attachments(content: str, rel_paths: Sequence[str]) -> str:
    """把附件清单拼到用户说的话后面（只有附件时，正文就是清单）。"""
    block = attachment_block(rel_paths)
    if not block:
        return content
    text = content.strip()
    return f"{text}\n\n{block}" if text else block
