"""用户口令的哈希与校验、无状态会话令牌（JWT）的签发与解析。

刻意不引入 ``bcrypt`` / ``pyjwt`` 等新依赖（铁律 12），改用已有的 ``cryptography``：

* 口令哈希用 PBKDF2-HMAC-SHA256（``cryptography`` 已带入），迭代次数与盐长度见
  :data:`PBKDF2_ITERATIONS` / :data:`SALT_BYTES`。存储形如 ``pbkdf2$<迭代>$<盐 b64>$<哈希 b64>``，
  校验时恒定时间比较。
* 会话令牌是一个最小的 JWT：头部固定 ``HS256``，签名密钥由 :mod:`app.security` 的
  Fernet 密钥再派生一层（sha256），与密钥文件解耦但同源。只签 ``sub``（用户 id）、
  ``role``、``exp`` 三个声明，不引入额外字段。

明文口令永不入库、不进日志、不进接口响应。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .security import _load_key

#: PBKDF2 迭代次数：与 OWASP 2023 推荐的下限对齐（>= 600_000），兼顾登录时的一次性开销。
PBKDF2_ITERATIONS = 600_000
#: 盐字节数：16 字节（128 位）足够且不至于让存储显得臃肿。
SALT_BYTES = 16
#: 会话令牌有效期（秒）：7 天。控制台是长会话形态，太短会频繁掉登录。
TOKEN_TTL_SECONDS = 7 * 24 * 3600


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(password: str) -> str:
    """把明文口令哈希成可存储的字符串。

    每次调用都生成新盐，因此同一口令两次哈希的结果不同；校验请用 :func:`verify_password`。
    """
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    digest = kdf.derive(password.encode("utf-8"))
    return f"pbkdf2${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """恒定时间校验口令。``stored`` 为空或格式不符一律返回 False，不抛错。"""
    if not stored or not stored.startswith("pbkdf2$"):
        return False
    try:
        _scheme, iter_str, salt_b64, digest_b64 = stored.split("$")
        iterations = int(iter_str)
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
    except (ValueError, TypeError):
        return False
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=len(expected),
        salt=salt,
        iterations=iterations,
    )
    # verify 在不匹配时抛 InvalidSignature，这里要的是布尔结果。
    try:
        kdf.verify(password.encode("utf-8"), expected)
        return True
    except Exception:  # noqa: BLE001 - 口令不匹配、编码错误等都按校验失败处理
        return False


def _signing_key() -> bytes:
    """从 Fernet 密钥再派生一层 HMAC 签名密钥（同源、不引入新密钥文件）。"""
    return hashlib.sha256(b"arkswarm-jwt:" + _load_key()).digest()


def _b64json(obj: Any) -> str:
    return _b64(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def issue_token(
    user_id: str, role: str, token_version: int = 0, *, ttl: int = TOKEN_TTL_SECONDS
) -> str:
    """签发一个会话令牌（JWT，HS256）；``token_version`` 与用户当前版本一致才有效。"""
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {"sub": user_id, "role": role, "ver": token_version, "iat": now, "exp": now + ttl}
    signing_input = f"{_b64json(header)}.{_b64json(payload)}"
    sig = hmac.new(_signing_key(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


class TokenError(Exception):
    """令牌解析失败（过期、签名不符、格式错误）。"""


def parse_token(token: str) -> dict[str, Any]:
    """解析并校验令牌，返回 payload；失败抛 :class:`TokenError`。"""
    if not token or token.count(".") != 2:
        raise TokenError("令牌格式不正确")
    signing_input, sig_b64 = token.rsplit(".", 1)
    try:
        # 头部/载荷必须能被 ASCII 编码，签名必须是合法 base64url；
        # 任何畸形输入都按「令牌格式不正确」处理，不能变成 500。
        signing_bytes = signing_input.encode("ascii")
        provided_sig = _unb64(sig_b64)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise TokenError("令牌格式不正确") from exc
    expected_sig = hmac.new(_signing_key(), signing_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise TokenError("令牌签名不匹配")
    try:
        payload = json.loads(_unb64(signing_input.split(".", 1)[1]))
    except (ValueError, TypeError) as exc:
        raise TokenError("令牌载荷无法解析") from exc
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise TokenError("令牌已过期")
    return payload