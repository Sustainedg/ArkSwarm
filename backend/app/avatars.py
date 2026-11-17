"""用户头像：校验、落盘、读取。

头像和「对话附件」是两回事：附件属于某次对话的工作目录，头像属于**用户**本身，
跟着账号走（侧边栏、用户列表、协作消息里都要显示同一个人）。所以单独一个目录
``<数据目录>/avatars/``，文件名带用户 id 与内容摘要，换头像即换文件名 —— 浏览器
缓存自然失效，不需要额外的版本号。

三条硬约束：

* **只收位图**（PNG / JPEG / WebP）。SVG 能内嵌脚本，而头像会被 `<img>` 直接渲染，
  一旦允许上传就等于给所有登录用户开了一个存储型 XSS 的口子。
* **大小上限 2 MB**：头像是 64~128px 的小图，2 MB 已经足够宽容。
* **文件名由服务端生成**，绝不采信客户端给的名字（目录穿越、同名覆盖都在这里挡掉）。
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

#: 单张头像的上限
MAX_AVATAR_BYTES = 2 * 1024 * 1024

#: 头像目录名（相对数据目录）
AVATAR_DIR = "avatars"

#: 允许的位图格式：魔数 → 扩展名。顺序即检测顺序。
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"RIFF", "webp"),  # 还需在偏移 8 处确认 "WEBP"
)

#: 库里的文件名只允许这一种形状（``<32位hex>-<12位hex>.<ext>``）
_FILENAME = re.compile(r"^[0-9a-f]{32}-[0-9a-f]{12}\.(?:png|jpg|webp)$")


class AvatarError(ValueError):
    """头像不合法（不是图片 / 太大 / 空内容）。消息面向用户，可直接展示。"""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class StoredAvatar:
    """一张已经落盘的头像。"""

    filename: str
    path: Path
    size: int

    def to_dict(self) -> dict:
        return {"avatar": self.filename, "size": self.size}


def detect_image_type(raw: bytes) -> str | None:
    """按魔数认格式；认不出（或认成 SVG）返回 ``None``。"""
    for signature, extension in _SIGNATURES:
        if not raw.startswith(signature):
            continue
        if extension == "webp" and raw[8:12] != b"WEBP":
            continue
        return extension
    return None


def decode_avatar(content_base64: str, limit: int = MAX_AVATAR_BYTES) -> tuple[bytes, str]:
    """解开 base64、校验大小与格式，返回 ``(原始字节, 扩展名)``。"""
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (ValueError, TypeError):
        raise AvatarError("头像内容不是合法的 base64") from None
    if not raw:
        raise AvatarError("头像内容为空")
    if len(raw) > limit:
        raise AvatarError(
            f"头像过大：不能超过 {limit // 1024 // 1024} MB", status_code=413
        )
    extension = detect_image_type(raw)
    if extension is None:
        raise AvatarError("头像只支持 PNG / JPEG / WebP 图片")
    return raw, extension


def avatar_dir(data_dir: Path) -> Path:
    return Path(data_dir) / AVATAR_DIR


def store_avatar(data_dir: Path, user_id: str, raw: bytes, extension: str) -> StoredAvatar:
    """写入 ``<数据目录>/avatars/<用户id>-<内容摘要>.<ext>``，并清掉该用户的其他旧头像。

    文件名里带内容摘要：同一张图重复上传不会产生新文件，换了图则文件名必变，
    前端拿到的 URL / 缓存键自动失效。
    """
    directory = avatar_dir(data_dir)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    filename = f"{user_id}-{digest}.{extension}"
    target = directory / filename
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            target.write_bytes(raw)
        _remove_other_avatars(directory, user_id, keep=filename)
    except OSError as exc:
        raise AvatarError(f"头像保存失败：{exc}", status_code=500) from exc
    return StoredAvatar(filename=filename, path=target, size=len(raw))


def _remove_other_avatars(directory: Path, user_id: str, keep: str) -> None:
    """删掉这个用户的旧头像文件；删不掉就算了（不影响本次保存）。"""
    for candidate in directory.glob(f"{user_id}-*"):
        if candidate.name == keep or not candidate.is_file():
            continue
        try:
            candidate.unlink()
        except OSError:
            pass


def remove_avatar(data_dir: Path, filename: str | None) -> None:
    """删除一张头像文件；文件名不合法或已不存在都静默跳过。"""
    if not filename or not _FILENAME.match(filename):
        return
    try:
        (avatar_dir(data_dir) / filename).unlink()
    except OSError:
        pass


#: 扩展名 → MIME：只认这三种（上传时就限定了），不必走 mimetypes 猜。
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def guess_media_type(filename: str) -> str:
    return MEDIA_TYPES.get(Path(filename).suffix.lstrip(".").lower(), "application/octet-stream")


def resolve_avatar(data_dir: Path, filename: str | None) -> Path | None:
    """把库里的文件名解析成真实路径；文件不在了返回 ``None``（界面回退到首字母）。"""
    if not filename or not _FILENAME.match(filename):
        return None
    target = avatar_dir(data_dir) / filename
    return target if target.is_file() else None
