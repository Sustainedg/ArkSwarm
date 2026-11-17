"""各平台的收发适配器。

新增一个平台 = 在 ``base.PLATFORMS`` 加一条元数据 + 在这里注册一个类。
"""
from __future__ import annotations

from ..base import Adapter
from .dingtalk import DingTalkAdapter
from .discord import DiscordAdapter
from .feishu import FeishuAdapter
from .qq import QQAdapter
from .slack import SlackAdapter
from .telegram import TelegramAdapter
from .wecom import WeComAdapter

#: 平台 id → 适配器实例。``base.PLATFORMS`` 里 ``available=False`` 的平台不在这里。
ADAPTERS: dict[str, Adapter] = {
    adapter.id: adapter
    for adapter in (
        TelegramAdapter(),
        FeishuAdapter(),
        DingTalkAdapter(),
        WeComAdapter(),
        QQAdapter(),
        DiscordAdapter(),
        SlackAdapter(),
    )
}


def adapter_for(platform_id: str) -> Adapter | None:
    return ADAPTERS.get(platform_id)


__all__ = ["ADAPTERS", "adapter_for"]
