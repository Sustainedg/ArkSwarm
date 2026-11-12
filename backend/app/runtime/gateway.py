"""网关 → 一次运行的绑定：注入哪个地址、哪把密钥、用哪种鉴权头。

**官方链路 = 平台不接管路由。** ``kind="official"`` 的行只承载适配器声明的模型清单
（`default_models`），运行时不注入地址与密钥 —— 与「没选网关」完全同义，由内核按自己的
配置 / 环境处理。只有用户自建的 ``kind="custom"`` 网关才会被注入，密钥一律只经
`app/security.py` 解密，禁止出现在日志、事件与响应里。
"""
from __future__ import annotations

from dataclasses import dataclass

from ..agents.base import AgentDefinition
from ..models import Gateway
from ..security import decrypt


@dataclass(slots=True)
class GatewayBinding:
    """一次运行实际生效的网关三件套。"""

    url: str | None = None
    api_key: str | None = None
    auth_style: str | None = None


def bind_gateway(definition: AgentDefinition, gateway: Gateway | None) -> GatewayBinding:
    """解析一次运行要注入的网关地址 / 密钥 / 鉴权方式。"""
    if gateway is None or gateway.kind != "custom":
        # 没选网关，或选的是官方链路：平台不接管路由，回落到适配器读到的本机配置。
        return GatewayBinding(url=definition.resolve_gateway(None))
    return GatewayBinding(
        url=gateway.base_url,
        api_key=decrypt(gateway.api_key_encrypted),
        auth_style=gateway.auth_style,
    )


__all__ = ["GatewayBinding", "bind_gateway"]
