"""网络出口过滤：限制智能体子进程与 API 内核能连到哪些主机。

信任边界声明：有 shell 访问或网络访问的智能体原则上能读到注入的凭据值。
平台做了多层防护（加密落库 → 只以环境变量注入 → 事件流擦除 → 命令回显脱敏 → 接口只回掩码），
但无法阻止一个有 shell + 网络的 AI 把信息间接传出去。网络出口过滤是堵住外泄通道的关键一层。

三种模式（由 ``MAGENTA_EGRESS_MODE`` 设置）：

* ``off``（默认）：不限制，保持向后兼容；
* ``block``：禁止所有出站网络 —— 子进程注入 blackhole 代理环境变量，
  API 内核的 httpx 客户端用 :class:`EgressTransport` 包装后拒绝所有连接；
* ``allowlist``：只允许白名单里的域名（网关地址自动加入白名单）。

本模块是叶子工具（对话域与协作域共用一份），只依赖标准库与 ``settings``。
"""
from __future__ import annotations

from typing import Iterable
from urllib.parse import urlparse

import httpx

from .settings import get_settings

#: 不可达的 blackhole 代理地址：子进程里设了它之后，任何 HTTP 客户端
#: 都会先尝试连这个端口、立刻被拒绝（Connection refused），从而阻断出站请求。
_BLACKHOLE_PROXY = "http://127.0.0.1:9"

#: 注入给子进程的代理环境变量名（覆盖 curl / wget / httpx / requests / Node fetch 等常见客户端）
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

#: block / allowlist 模式下都要始终放行的本地地址（模型 API 不走这里）
_ALWAYS_LOCAL = ("127.0.0.1", "localhost", "0.0.0.0")


def egress_mode() -> str:
    """当前出口过滤模式：``off`` / ``block`` / ``allowlist``。"""
    return get_settings().egress_mode


def allowed_hosts(extra: Iterable[str] = ()) -> set[str]:
    """本次运行的白名单主机集合。

    ``block`` 模式返回空集（什么都不放行）；
    ``allowlist`` 模式返回设置里的白名单 + 调用方追加的（如网关地址）；
    ``off`` 模式返回空集（调用方据 ``egress_mode() == "off"`` 判断不限制）。
    """
    mode = egress_mode()
    if mode == "off":
        return set()
    if mode == "block":
        return set()
    hosts = set(get_settings().egress_allowed_hosts_list)
    for item in extra:
        if item:
            host = urlparse(str(item)).hostname or str(item)
            hosts.add(host)
    return hosts


def proxy_env_for_subprocess() -> dict[str, str]:
    """注入给 CLI 子进程的代理环境变量。

    ``off`` 模式返回空字典（不干预）；
    ``block`` / ``allowlist`` 模式都注入 blackhole 代理 —— ``allowlist`` 的放行
    靠 API 内核侧的 :class:`EgressTransport` 精确控制，子进程侧无法做域名级过滤，
    因此一律阻断（需要联网的 CLI 内核应配 ``off`` 模式或在白名单里用代理中转）。
    """
    if egress_mode() == "off":
        return {}
    return {key: _BLACKHOLE_PROXY for key in _PROXY_ENV_KEYS}


def host_allowed(url: str, extra_hosts: Iterable[str] = ()) -> bool:
    """检查一个 URL 的主机是否在白名单里。

    ``off`` 模式一律放行；``block`` 一律拒绝；``allowlist`` 看白名单。
    """
    mode = egress_mode()
    if mode == "off":
        return True
    if mode == "block":
        return False
    host = urlparse(url).hostname or ""
    if host in _ALWAYS_LOCAL:
        return True
    return host in allowed_hosts(extra_hosts)


def blocked_message(url: str) -> str:
    """给用户看的「被出口策略拦截」提示。"""
    mode = egress_mode()
    if mode == "block":
        return "网络出口已设为全部阻断（MAGENTA_EGRESS_MODE=block），不允许任何出站连接"
    host = urlparse(url).hostname or url
    return f"网络出口白名单不包含 {host}（MAGENTA_EGRESS_ALLOWED_HOSTS），连接被拒绝"


class EgressTransport(httpx.AsyncBaseTransport):
    """包装一个 httpx 异步传输，按出口策略拦截非白名单连接。

    ``off`` 模式直接透传（不拦截）；``block`` / ``allowlist`` 模式检查每个请求的
    目标主机，不在白名单里的返回 ``ConnectError``，在白名单里的放行给底层传输。
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, extra_hosts: Iterable[str] = ()) -> None:
        self._inner = inner
        self._extra = tuple(extra_hosts)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not host_allowed(url, self._extra):
            raise httpx.ConnectError(blocked_message(url), request=request)
        return await self._inner.handle_async_request(request)


def wrap_client(client: httpx.AsyncClient, extra_hosts: Iterable[str] = ()) -> httpx.AsyncClient:
    """给一个 httpx 客户端套上出口过滤传输层（off 模式原样返回）。"""
    if egress_mode() == "off":
        return client
    original_transport = client._transport
    if original_transport is None:
        return client
    client._transport = EgressTransport(original_transport, extra_hosts)
    return client


__all__ = [
    "EgressTransport",
    "blocked_message",
    "egress_mode",
    "host_allowed",
    "allowed_hosts",
    "proxy_env_for_subprocess",
    "wrap_client",
]
