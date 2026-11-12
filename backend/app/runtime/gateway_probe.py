"""按网关协议探测可用模型。

「怎么列模型」是**网关协议**（openai / anthropic / gemini）的知识，不是内核的知识，
所以它落在框架层；内核只负责声明自己的官方链路走哪种协议（AgentDefinition.protocol）。
探测一律尽力而为：失败返回中文原因，由界面提示用户改用手动录入（「该地址不提供
/models」旁路），绝不把异常穿透到接口层。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("magenta")

#: 支持的网关协议
PROTOCOLS = ("openai", "anthropic", "gemini")
#: 支持的鉴权方式
AUTH_STYLES = ("bearer", "x-api-key", "query", "none")

PROTOCOL_LABELS = {
    "openai": "OpenAI 兼容（/v1/models）",
    "anthropic": "Anthropic（/v1/models）",
    "gemini": "Gemini（/v1beta/models）",
}
AUTH_LABELS = {
    "bearer": "Bearer（Authorization 头）",
    "x-api-key": "x-api-key 头",
    "query": "查询参数 key=",
    "none": "无需密钥",
}

TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class ProbeError(Exception):
    """探测失败：消息面向用户。"""


def _headers(
    protocol: str, auth_style: str, api_key: str | None, extra: dict[str, Any] | None
) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    for key, value in (extra or {}).items():
        headers[str(key)] = str(value)
    if api_key:
        if auth_style == "bearer":
            headers.setdefault("Authorization", f"Bearer {api_key}")
        elif auth_style == "x-api-key":
            headers.setdefault("x-api-key", api_key)
    if protocol == "anthropic":
        # Anthropic 要求显式协议版本头，缺了会 400。
        headers.setdefault("anthropic-version", "2023-06-01")
    return headers


def models_url(base_url: str, protocol: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        raise ProbeError("请填写网关地址")
    if protocol == "gemini":
        if base.endswith("/models"):
            return base
        return f"{base}/v1beta/models"
    if base.endswith("/models"):
        return base
    if base.endswith("/chat/completions"):
        return base[: -len("/chat/completions")] + "/models"
    return f"{base}/models"


def _parse(protocol: str, payload: Any) -> list[str]:
    if protocol == "gemini":
        rows = (payload or {}).get("models") if isinstance(payload, dict) else None
        names: list[str] = []
        for row in rows or []:
            name = str((row or {}).get("name") or "") if isinstance(row, dict) else ""
            if name:
                names.append(name.removeprefix("models/"))
        return names
    rows = (payload or {}).get("data") if isinstance(payload, dict) else None
    names = []
    for row in rows or []:
        value = (row or {}).get("id") if isinstance(row, dict) else row
        if value:
            names.append(str(value))
    return names


async def probe_models(
    *,
    base_url: str,
    protocol: str = "openai",
    auth_style: str = "bearer",
    api_key: str | None = None,
    extra_headers: dict[str, Any] | None = None,
) -> list[str]:
    """拉取网关的模型清单；失败抛 ProbeError（消息面向用户）。"""
    protocol = protocol if protocol in PROTOCOLS else "openai"
    auth_style = auth_style if auth_style in AUTH_STYLES else "bearer"
    url = models_url(base_url, protocol)
    params: dict[str, str] = {}
    if api_key and auth_style == "query":
        params["key"] = api_key
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                url, headers=_headers(protocol, auth_style, api_key, extra_headers), params=params
            )
    except httpx.HTTPError as exc:
        raise ProbeError(f"无法连接网关：{exc}") from exc
    if response.status_code >= 400:
        detail = (response.text or "").strip().replace("\n", " ")[:200]
        raise ProbeError(f"网关返回 HTTP {response.status_code}：{detail or '无响应正文'}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProbeError("网关返回的不是 JSON，无法解析模型清单") from exc
    models = _parse(protocol, payload)
    if not models:
        raise ProbeError("网关的返回里没有模型；如果该地址不提供 /models，请改用手动录入")
    return sorted(set(models))
