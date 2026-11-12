"""在宿主机上安装 / 更新 / 卸载智能体运行时（CLI 内核）。

安装与卸载是「改本机环境」的长任务，因此与一次对话运行同等对待：
受控子进程、逐行流式回显、总超时、命令脱敏；客户端断开连接时生成器被关闭，
``finally`` 里杀掉子进程——与对话一样天然可取消（铁律 10）。

本模块只认识适配器声明的 :class:`~app.agents.base.RuntimeProvisioner`，
不知道任何具体内核：装哪个包、用什么包管理器、动作前要清掉什么残留，
都是适配器的知识（``prepare_argv``）—— 这里只负责照声明执行。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal

from ..agents.base import AgentDefinition, RuntimeProvisioner
from ..settings import get_settings
from . import availability as availability_service
from .runner import _StreamLines, _redact

Action = Literal["install", "update", "uninstall"]

#: 动作的中文名，用于产出面向用户的提示。
ACTION_LABELS: dict[str, str] = {"install": "安装", "update": "更新", "uninstall": "卸载"}

#: 「查询最新可用版本」是很轻的元数据请求，给它一个单独的短超时。
_LATEST_TIMEOUT_SECONDS = 30.0

#: 失败提示里回显命令的长度上限。
_HINT_COMMAND_LIMIT = 120


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    """产出一条 ``provision.*`` 事件。

    这是安装器自有的信封，**不是**适配器事件协议（铁律 5 不适用于它），
    与协作域 ``collab.*`` 同理：只在运行时页内部消费。
    """
    return {"type": f"provision.{kind}", **payload}


def _display(argv: list[str]) -> str:
    """把命令拼成一行展示。

    包装命令可能是多行的 shell 脚本（例如 npm 内核的安装 / 更新命令要先处理本机既有的
    同名可执行文件），压成一行才不会把日志与提示切断。
    """
    return " ".join(" ".join(argv).split())


def _hint(display: str) -> str:
    """失败提示里回显命令：太长就截断（完整命令在日志与 ``provision.started`` 里）。"""
    return display if len(display) <= _HINT_COMMAND_LIMIT else f"{display[:_HINT_COMMAND_LIMIT]}…"


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


@dataclass(slots=True)
class _CommandOutcome:
    """一条受控命令的执行结论。"""

    returncode: int | None = None
    #: 超过了本次动作的总时限，进程被强制终止
    timed_out: bool = False
    #: 命令根本没启动起来时的现成文案（已含动作名，面向用户）；正常启动则为 None
    error: str | None = None


async def _run_command(
    argv: list[str],
    env: dict[str, str],
    deadline: float,
    runtime_id: str,
    label: str,
    outcome: _CommandOutcome,
) -> AsyncIterator[dict[str, Any]]:
    """在受控子进程里跑一条命令，逐行产出 ``provision.output``。

    结论（退出码 / 是否超时 / 是否压根没起来）写进调用方传入的 ``outcome``。
    这里**不**产出 ``provision.finished``：收尾要重测安装状态、判定成败，由调用方决定。
    超时会杀掉子进程；客户端断开时生成器被关闭，``finally`` 同样杀掉它（铁律 10）。
    """
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
        outcome.error = f"未找到命令 `{argv[0]}`，无法执行{label}"
        return
    except PermissionError as exc:
        outcome.error = f"无法执行 `{argv[0]}`：{exc}"
        return

    try:
        assert process.stdout is not None
        lines = _StreamLines(process.stdout)
        while True:
            if asyncio.get_event_loop().time() > deadline:
                outcome.timed_out = True
                process.kill()
                break
            try:
                line = await asyncio.wait_for(lines.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if lines.overflowed:
                lines.overflowed = False
                yield _event("output", runtime_id=runtime_id, text="单行输出过长，已丢弃该行", level="warn")
            if line is None:
                break
            yield _event("output", runtime_id=runtime_id, text=line.decode("utf-8", "replace"))
        await process.wait()
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
    outcome.returncode = process.returncode


async def stream_provision(definition: AgentDefinition, action: Action) -> AsyncIterator[dict[str, Any]]:
    """执行一次安装 / 更新 / 卸载，并流式回显输出。

    终态始终是一条 ``provision.finished`` 事件（含 ``ok`` / ``status`` / ``version``），
    运行过程中是若干 ``provision.output``。缺命令等前置错误同样以 ``finished`` 收尾，
    保证调用方永远能看到结论（铁律 11）。

    适配器声明了 ``prepare_argv`` 时，先跑一遍清扫命令（清掉上一次被中断的动作留下的
    残留），再跑动作本身；两者共用同一个总时限。
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

    timeout = float(get_settings().provision_timeout_seconds)
    deadline = asyncio.get_event_loop().time() + timeout

    # 动作之前先跑适配器声明的「清扫」命令：上一次动作被中断（超时、服务重启）可能留下
    # 让包管理器再也无法覆写安装目录的残留（见 `RuntimeProvisioner.prepare_argv`）。
    # 它只是清场，失败了也照常往下走 —— 成败只由动作自己的退出码决定。
    if provisioner.prepare_argv:
        prepare_display = _display(_redact(provisioner.prepare_argv, env))
        yield _event("output", runtime_id=definition.id, text=f"清扫命令：{prepare_display}")
        prepare = _CommandOutcome()
        # aclosing：客户端断开时框架关闭的是本生成器，`async for` 不像 `yield from`
        # 那样代为关闭被迭代的生成器 —— 必须显式透传，否则子进程成孤儿（铁律 10）。
        async with contextlib.aclosing(
            _run_command(provisioner.prepare_argv, env, deadline, definition.id, "清扫", prepare)
        ) as prepare_stream:
            async for event in prepare_stream:
                yield event
        if prepare.error or prepare.timed_out or prepare.returncode:
            reason = prepare.error or ("超时" if prepare.timed_out else f"退出码 {prepare.returncode}")
            yield _event(
                "output",
                runtime_id=definition.id,
                text=f"清扫未完成（{reason}），继续执行{label}",
                level="warn",
            )

    outcome = _CommandOutcome()
    async with contextlib.aclosing(
        _run_command(argv, env, deadline, definition.id, label, outcome)
    ) as command_stream:
        async for event in command_stream:
            yield event
    if outcome.error:
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=outcome.error,
        )
        return

    if outcome.timed_out:
        yield _event(
            "output",
            runtime_id=definition.id,
            text=f"已超过 {int(timeout)} 秒上限，被强制终止",
            level="warn",
        )

    # 安装 / 更新 / 卸载都会动本机那份可执行文件，所以这里顺手重测一遍缓存
    # （探测在后台线程里跑，不占用事件循环），并用新结论收尾。
    status = await availability_service.refresh_definition(definition)
    ok = outcome.returncode == 0 and not outcome.timed_out
    yield _event(
        "finished",
        runtime_id=definition.id,
        action=action,
        ok=ok,
        status="ok" if ok else "error",
        exit_code=outcome.returncode,
        installed=status.installed,
        version=status.version,
        path=status.detail if status.installed else None,
        error=None if ok else f"`{_hint(display)}` 退出码 {outcome.returncode}",
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
