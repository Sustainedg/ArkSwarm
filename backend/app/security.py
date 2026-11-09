"""API Key 静态加密存储，且接口永不返回明文。"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .settings import get_settings

_key_cache: bytes | None = None


def _load_key() -> bytes:
    global _key_cache
    if _key_cache:
        return _key_cache

    settings = get_settings()
    if settings.secret_key:
        digest = hashlib.sha256(settings.secret_key.encode()).digest()
        _key_cache = base64.urlsafe_b64encode(digest)
        return _key_cache

    key_file = Path(settings.data_dir) / "secret.key"
    if key_file.exists():
        _key_cache = key_file.read_bytes().strip()
    else:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        try:
            os.chmod(key_file, 0o600)
        except OSError:  # best effort on exotic filesystems
            pass
        _key_cache = key
    return _key_cache


def _fernet() -> Fernet:
    return Fernet(_load_key())


def encrypt(plaintext: str | None) -> str | None:
    if not plaintext:
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask(secret: str | None) -> str | None:
    """生成给界面展示的掩码，例如 ``sk-…9f2c``。"""
    if not secret:
        return None
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}…{secret[-4:]}"
