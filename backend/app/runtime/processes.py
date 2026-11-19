"""结束内核子进程：POSIX 直杀，Windows 连进程树一起杀。

为什么需要单独一份：桌面版（M4）起 Windows 内核时，npm 生成的 ``.cmd`` 垫片要经
``cmd.exe /d /s /c`` 启动（``runner._spawn_argv``）—— 此时 ``process`` 的 pid 是
cmd.exe 的，直接 ``process.kill()`` 只会杀掉壳，真正的 node 内核会变成孤儿进程
继续跑（取消没生效、还占着模型配额）。所以 Windows 上一律用 ``taskkill /T /F``
按进程树清理。

Popen / create_subprocess_exec 都是按程序名启动，不经 shell；pid 是整数，没有注入面。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from typing import Any


def _windows_tree(pid: int) -> list[str]:
    return ["taskkill", "/PID", str(pid), "/T", "/F"]


def kill_tree(process: Any) -> None:
    """同步结束进程（``manager.cancel()`` 用）：Windows 只负责把 taskkill 发出去，不阻塞。"""
    if process is None or process.returncode is not None:
        return
    pid = getattr(process, "pid", None)
    if os.name == "nt" and pid:
        try:
            subprocess.Popen(
                _windows_tree(int(pid)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    with contextlib.suppress(ProcessLookupError):
        process.kill()


async def kill_tree_async(process: Any, *, timeout: float = 10.0) -> None:
    """异步结束进程（执行层用）：Windows 等 taskkill 落地，确保树杀干净再继续收尾。"""
    if process is None or process.returncode is not None:
        return
    pid = getattr(process, "pid", None)
    if os.name == "nt" and pid:
        try:
            killer = await asyncio.create_subprocess_exec(
                *_windows_tree(int(pid)),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=timeout)
            return
        except (OSError, asyncio.TimeoutError):
            pass
    with contextlib.suppress(ProcessLookupError):
        process.kill()