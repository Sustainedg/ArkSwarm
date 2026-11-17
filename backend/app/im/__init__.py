"""IM 域：在聊天软件里和智能体说话。

对外只暴露两样东西：``app.api`` 挂载的 ``api.router``，以及应用生命周期用的
``service.manager``。平台收发在 ``platforms/``，数据表在 ``tables.py``，
与对话域的唯一交界是 ``bridge.py``。
"""
from __future__ import annotations

from . import tables  # noqa: F401  （注册表结构）
from .service import manager

#: 全局唯一的连接管理器实例：应用生命周期用它启停连接。
im_manager = manager()

__all__ = ["im_manager", "manager"]
