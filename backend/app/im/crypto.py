"""IM 平台需要的少量密码学工具（目前只有企业微信的媒体 AES 解密）。

单独成一个叶子模块，是为了让「平台适配器只依赖 base / format / crypto + 标准库 +
httpx / websockets」这条依赖清单保持精确：``cryptography`` 只在这里出现，适配器
不直接 import 它。企业微信的媒体下载必须用 AES-256-CBC 解密，这是协议要求，
不是平台业务逻辑，所以放在通用工具里而不是某个平台模块内。
"""
from __future__ import annotations

import base64
import binascii

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def b64(value: object) -> bytes:
    """宽松地解 base64：任何非法输入都返回空串，由调用方按「没有密钥」处理。"""
    try:
        return base64.b64decode(str(value or ""))
    except (ValueError, TypeError, binascii.Error):
        return b""


def decrypt_aes256_cbc(encrypted: bytes, key: bytes) -> bytes:
    """企业微信按 32 字节块补位（不是 16），所以自己摘掉填充。"""
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = decryptor.update(encrypted) + decryptor.finalize()
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 32 and pad <= len(plain) and plain[-pad:] == bytes([pad]) * pad:
        return plain[:-pad]
    return plain


__all__ = ["b64", "decrypt_aes256_cbc"]
