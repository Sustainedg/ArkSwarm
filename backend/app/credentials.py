"""凭据：本地加密保存，运行时以环境变量注入，永不进日志 / 事件 / 接口响应。

「凭据」（credentials）是智能体干活时需要的第三方密钥（GitHub token、数据库口令、部署密钥…），
**与「网关密钥」不是一回事**：网关密钥由 :class:`~app.models.Gateway` 唯一持有（模型提供方），
凭据属于工具与服务，**全局一份**（``credentials`` 表）并可被多个智能体引用
（``agent_credentials``）：在「设置 → 凭据库」里维护，或直接在智能体配置里挑一条 / 新建一条。
两者都只经 :mod:`app.security` 加解密。

为什么用环境变量注入而不是拼进提示词：所有内核（Claude Code / Codex / Gemini CLI /
custom-cli / 进程内 API）都能读环境变量，**值**不会出现在提示词、对话记录与接口响应里，
模型只知道变量的名字；界面只看到掩码（``sk-…9f2c``）。运行层还会把凭据值从事件文本里
擦掉（``scrub_event``），并让命令回显脱敏（``runner._redact``）。

本模块是叶子工具（对话域与协作域共用一份），只依赖 :mod:`app.security`。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from .security import decrypt, encrypt, mask

#: 凭据名要能直接当环境变量名用，否则注入后内核读不到。
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: 凭据的只读视图（界面 / 接口用）：``{id, name, description, has_value, masked}``。


class CredentialError(ValueError):
    """凭据不合法（名字不是合法环境变量名 / 重名）。

    消息面向用户、可直接展示；``status_code`` 交给 API 层当 HTTP 状态码用。
    """

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_name(raw: str) -> str:
    """规整并校验凭据名；不合法时抛出可直接展示的中文错误。"""
    name = (raw or "").strip()
    if not ENV_NAME_PATTERN.match(name):
        raise CredentialError(
            f"凭据名「{raw}」不能用作环境变量名：请以字母或下划线开头，"
            "只使用字母、数字与下划线（例如 GITHUB_TOKEN）"
        )
    return name


def clean_description(raw: Any) -> str | None:
    """规整说明文本：空串归一成 ``None``。"""
    text = (raw or "").strip() if isinstance(raw, str) else None
    return text or None


def decrypt_all(rows: Sequence[Any] | None) -> dict[str, str]:
    """把凭据行解密成「环境变量名 → 明文」；解不开或空值的行直接跳过（绝不抛错中断运行）。"""
    out: dict[str, str] = {}
    for row in rows or []:
        name = getattr(row, "name", None)
        if not name:
            continue
        value = decrypt(getattr(row, "value_encrypted", None))
        if value:
            out[str(name)] = value
    return out


def reads(rows: Sequence[Any] | None) -> list[dict[str, Any]]:
    """给界面的只读视图：只有名字、说明与掩码，永远没有明文。"""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        value = decrypt(getattr(row, "value_encrypted", None))
        out.append(
            {
                "id": getattr(row, "id", None),
                "name": getattr(row, "name", ""),
                "description": getattr(row, "description", None),
                "has_value": bool(value),
                "masked": mask(value),
            }
        )
    return out


def notice(credentials: dict[str, str]) -> str | None:
    """拼给模型的「可用凭据」说明：**只有名字**，值一律不进提示词。"""
    if not credentials:
        return None
    lines = "\n".join(f"- ${name}" for name in credentials)
    return (
        "🔑 本次运行已注入以下凭据（仅以环境变量形式提供，直接读取即可，"
        "不要在回复里打印它们的值）：\n"
        f"{lines}"
    )


def with_notice(system_prompt: str | None, credentials: dict[str, str]) -> str | None:
    """把「可用凭据」说明并进人格文本；没有凭据时原样返回。"""
    block = notice(credentials)
    if not block:
        return system_prompt or None
    return f"{system_prompt}\n\n{block}" if system_prompt else block


def scrub(text: str | None, secrets: Iterable[str]) -> str | None:
    """把文本里出现的凭据值抹成 ``***``（命令回显、日志与模型输出共用一份实现）。"""
    if not text:
        return text
    out = text
    for secret in secrets:
        if secret and secret in out:
            out = out.replace(secret, "***")
    return out


def scrub_event(event: Any, secrets: Iterable[str]) -> Any:
    """把一条事件里所有字符串（``text`` 与 ``payload``）中的凭据值擦掉。

    运行层对每条事件都过一遍，因此凭据即使被子进程回显也进不了 SSE 与数据库。
    """
    values = [value for value in secrets if value]
    if not values:
        return event
    return type(event)(
        type=event.type,
        text=scrub(event.text, values),
        name=scrub(event.name, values),
        call_id=event.call_id,
        payload=_scrub_payload(event.payload, values),
    )


def _scrub_payload(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, dict):
        return {key: _scrub_payload(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_payload(item, secrets) for item in value]
    return value


__all__ = [
    "CredentialError",
    "clean_description",
    "decrypt_all",
    "notice",
    "reads",
    "scrub",
    "scrub_event",
    "validate_name",
    "with_notice",
]
