"""在宿主机上安装 / 更新 / 卸载智能体运行时（CLI 内核）。

安装与卸载是「改本机环境」的长任务，因此与一次对话运行同等对待：
受控子进程、逐行流式回显、总超时、命令脱敏；客户端断开连接时生成器被关闭，
``finally`` 里杀掉子进程——与对话一样天然可取消（铁律 10）。

本模块只认识适配器声明的 :class:`~app.agents.base.RuntimeProvisioner`，
不知道任何具体内核：装哪个包、用什么包管理器都是适配器的知识。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any, AsyncIterator, Literal

from ..agents.base import AgentDefinition, RuntimeProvisioner
from ..settings import get_settings
from .runner import _StreamLines, _redact

Action = Literal["install", "update", "uninstall"]

#: 动作的中文名，用于产出面向用户的提示。
ACTION_LABELS: dict[str, str] = {"install": "安装", "update": "更新", "uninstall": "卸载"}

#: 「查询最新可用版本」是很轻的元数据请求，给它一个单独的短超时。
_LATEST_TIMEOUT_SECONDS = 30.0


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    """产出一条 ``provision.*`` 事件。

    这是安装器自有的信封，**不是**适配器事件协议（铁律 5 不适用于它），
    与协作域 ``collab.*`` 同理：只在运行时页内部消费。
    """
    return {"type": f"provision.{kind}", **payload}


def _display(argv: list[str]) -> str:
    return " ".join(argv)


def _command_env() -> dict[str, str]:
    """安装命令的运行环境：继承进程环境，并关掉交互式提示与彩色输出。"""
    env = {**os.environ}
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    return env


def _workdir() -> str | None:
    """在用户主目录下执行安装，避免包管理器把产物写进本仓库。"""
    home = os.environ.get("HOME")
    return home if home and os.path.isdir(home) else None


async def stream_provision(definition: AgentDefinition, action: Action) -> AsyncIterator[dict[str, Any]]:
    """执行一次安装 / 更新 / 卸载，并流式回显输出。

    终态始终是一条 ``provision.finished`` 事件（含 ``ok`` / ``status`` / ``version``），
    运行过程中是若干 ``provision.output``。缺命令等前置错误同样以 ``finished`` 收尾，
    保证调用方永远能看到结论（铁律 11）。
    """
    provisioner = definition.provisioner
    label = ACTION_LABELS.get(action, action)
    if provisioner is None:
        yield _event("started", runtime_id=definition.id, action=action, command=None)
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=f"{definition.name} 不由平台管理，无法{label}",
        )
        return

    argv = provisioner.argv_for(action)
    if not argv:
        yield _event("started", runtime_id=definition.id, action=action, command=None)
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=f"{definition.name} 未声明「{label}」命令",
        )
        return

    env = _command_env()
    display = _display(_redact(argv, env))
    yield _event(
        "started",
        runtime_id=definition.id,
        action=action,
        command=display,
        manager=provisioner.manager,
    )

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=_workdir(),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            # 合并 stderr 到 stdout：安装日志里两者交错，合并后既能保证顺序，
            # 也避免两条流各自读取带来的死锁风险。
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=f"未找到命令 `{argv[0]}`，无法执行{label}",
        )
        return
    except PermissionError as exc:
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=f"无法执行 `{argv[0]}`：{exc}",
        )
        return

    timeout = float(get_settings().provision_timeout_seconds)
    deadline = asyncio.get_event_loop().time() + timeout
    timed_out = False
    try:
        assert process.stdout is not None
        lines = _StreamLines(process.stdout)
        while True:
            if asyncio.get_event_loop().time() > deadline:
                timed_out = True
                process.kill()
                break
            try:
                line = await asyncio.wait_for(lines.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if lines.overflowed:
                lines.overflowed = False
                yield _event("output", runtime_id=definition.id, text="单行输出过长，已丢弃该行", level="warn")
            if line is None:
                break
            yield _event("output", runtime_id=definition.id, text=line.decode("utf-8", "replace"))
        await process.wait()
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)

    if timed_out:
        yield _event(
            "output",
            runtime_id=definition.id,
            text=f"已超过 {int(timeout)} 秒上限，被强制终止",
            level="warn",
        )

    availability = definition.availability()
    ok = process.returncode == 0 and not timed_out
    yield _event(
        "finished",
        runtime_id=definition.id,
        action=action,
        ok=ok,
        status="ok" if ok else "error",
        exit_code=process.returncode,
        installed=availability.installed,
        version=availability.version,
        path=availability.detail if availability.installed else None,
        error=None if ok else f"`{display}` 退出码 {process.returncode}",
    )


async def latest_version(definition: AgentDefinition) -> dict[str, Any]:
    """运行「查询最新版本」命令，返回远端可用的最新版本号。

    只是给界面「检查更新」按钮用的元数据探测，任何失败都如实回报，绝不抛出。
    """
    provisioner: RuntimeProvisioner | None = definition.provisioner
    if provisioner is None or not provisioner.latest_argv:
        return {"supported": False, "latest": None, "command": None, "error": "该运行时不支持查询最新版本"}

    argv = provisioner.latest_argv
    env = _command_env()
    command = _display(_redact(argv, env))
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=_workdir(),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return {"supported": True, "latest": None, "command": command, "error": f"未找到命令 `{argv[0]}`"}
    except PermissionError as exc:
        return {"supported": True, "latest": None, "command": command, "error": f"无法执行 `{argv[0]}`：{exc}"}

    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=_LATEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        return {"supported": True, "latest": None, "command": command, "error": "查询超时"}

    text = (out or b"").decode("utf-8", "replace").strip()
    latest = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), None)
    ok = process.returncode == 0 and latest is not None
    return {
        "supported": True,
        "latest": latest if ok else None,
        "command": command,
        "error": None if ok else (text[-400:] or f"查询命令退出码 {process.returncode}"),
    }
