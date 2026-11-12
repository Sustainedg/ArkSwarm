"""执行层：把 :class:`Invocation` 或 API 内核转换成规范化的 AgentEvent。"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator

from ..agents import AgentContext, AgentDefinition, AgentRun, Runtime
from ..agents.events import AgentEvent, error, log, run_finished, run_started
from ..credentials import scrub_event
from ..settings import get_settings
from .manager import ActiveRun


#: 读取子进程输出时每次取回的字节数。
_STREAM_CHUNK_SIZE = 1 << 16

#: 单行输出的内存上限。内核（如 Claude Code）会把整段代码或工具结果塞进一行 JSONL，
#: 远超 :class:`asyncio.StreamReader` 默认的 64 KiB，因此这里自己拆行并把水位放宽到 64 MiB；
#: 真越界时丢弃该行剩余内容而不是让整轮运行崩掉（铁律 10：长任务不能因一处噪声中断）。
_MAX_LINE_BYTES = 64 << 20


def _run_secrets(ctx: AgentContext) -> list[str]:
    """本次运行要从事件文本里擦掉的敏感值：智能体的凭据 + 网关 API 密钥。

    两者都可能被子进程回显（stderr、工具输出），因此都要过 :func:`scrub_event`，
    否则会经 log / error 事件进 SSE 与数据库（铁律 6：明文密钥禁止进日志 / SSE payload）。
    """
    secrets = list(ctx.credentials.values()) if ctx.credentials else []
    if ctx.api_key:
        secrets.append(ctx.api_key)
    return secrets


class _StreamLines:
    """按行切分子进程输出，单行长度不受 ``StreamReader`` 默认 64 KiB 缓冲上限约束。

    ``StreamReader.readline()`` 在单行超过缓冲区上限时会抛 ``ValueError``
    （消息即「Separator is found, but chunk is longer than limit」），一次工具调用就能踩中，
    所以框架层必须自己拆行，不能把内核输出的行长当成已知量。
    """

    __slots__ = ("_buffer", "_stream", "overflowed")

    def __init__(self, stream: asyncio.StreamReader) -> None:
        self._stream = stream
        self._buffer = bytearray()
        #: 上一次读取是否因单行过大丢弃了内容；调用方据此产出 log 事件，不静默吞掉。
        self.overflowed = False

    async def readline(self) -> bytes | None:
        """返回一行（不含换行符）；流已结束且无残留内容时返回 ``None``。"""
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 1]
                return line
            if len(self._buffer) >= _MAX_LINE_BYTES:
                await self._drop_overlong_line()
                continue
            chunk = await self._stream.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                if not self._buffer:
                    return None
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            self._buffer.extend(chunk)

    async def _drop_overlong_line(self) -> None:
        """丢弃超长行的剩余内容（读到下一处换行），保证内存占用有上限。"""
        self._buffer.clear()
        self.overflowed = True
        while True:
            chunk = await self._stream.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                return
            index = chunk.find(b"\n")
            if index >= 0:
                self._buffer.extend(chunk[index + 1 :])
                return


async def stream_run(
    definition: AgentDefinition,
    ctx: AgentContext,
    run: ActiveRun,
) -> AsyncIterator[AgentEvent]:
    """按 runtime 分发到 CLI 或 API 的执行路径。"""
    yield run_started(run.id)
    if definition.runtime is Runtime.API:
        settings = get_settings()
        deadline = asyncio.get_event_loop().time() + settings.run_timeout_seconds
        timed_out = False
        async for event in definition.stream(ctx):
            if run.cancel_event.is_set():
                break
            if asyncio.get_event_loop().time() > deadline:
                timed_out = True
                yield error(f"运行已超过 {settings.run_timeout_seconds} 秒上限，被强制终止")
                break
            yield scrub_event(event, _run_secrets(ctx))
        if timed_out:
            yield run_finished("error")
        else:
            yield run_finished("cancelled" if run.cancel_event.is_set() else "ok")
        return
    async for event in stream_cli_run(definition, ctx, run):
        yield event


async def stream_cli_run(
    definition: AgentDefinition,
    ctx: AgentContext,
    run: ActiveRun,
) -> AsyncIterator[AgentEvent]:
    agent_run: AgentRun = definition.create_run(ctx)
    try:
        invocation = agent_run.build_invocation()
    except Exception as exc:  # noqa: BLE001 - 把配置错误显式暴露给用户
        yield error(f"无法构建启动命令：{exc}")
        yield run_finished("error")
        return

    run_dir = Path(tempfile.mkdtemp(prefix=f"magenta-{run.id}-"))
    for relative, content in (invocation.files or {}).items():
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    cwd = invocation.cwd or ctx.workspace_dir or os.getcwd()
    Path(cwd).mkdir(parents=True, exist_ok=True)

    # 凭据按环境变量注入（值绝不进日志/事件）；适配器/网关给的同名变量优先级更高，
    # 网关始终是模型密钥的唯一来源。
    env = {**os.environ, **ctx.credentials, **invocation.env}
    env = {k: v.replace("{{RUN_DIR}}", str(run_dir)) for k, v in env.items()}

    executable = shutil.which(invocation.argv[0]) if invocation.argv else None
    if executable is None:
        yield log(f"正在启动 {invocation.argv[0]}", "info")
    preview = " ".join(_redact(invocation.argv, env, *_run_secrets(ctx)))
    if len(preview) > 400:
        preview = preview[:400] + " …"
    yield log(preview, "command")

    try:
        process = await asyncio.create_subprocess_exec(
            *invocation.argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if invocation.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        yield error(f"`{invocation.argv[0]}` 未安装或不在 PATH 中")
        yield run_finished("error")
        return
    except PermissionError as exc:
        yield error(f"无法执行 `{invocation.argv[0]}`：{exc}")
        yield run_finished("error")
        return

    run.process = process

    if invocation.stdin is not None and process.stdin is not None:
        try:
            process.stdin.write(invocation.stdin.encode())
            await process.stdin.drain()
            process.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    stderr_chunks: list[str] = []
    assert process.stderr is not None
    stderr_lines = _StreamLines(process.stderr)

    async def drain_stderr() -> None:
        while True:
            line = await stderr_lines.readline()
            if line is None:
                break
            stderr_chunks.append(line.decode("utf-8", "replace") + "\n")

    stderr_task = asyncio.create_task(drain_stderr())
    settings = get_settings()
    deadline = asyncio.get_event_loop().time() + settings.run_timeout_seconds
    cancelled = False

    try:
        assert process.stdout is not None
        stdout_lines = _StreamLines(process.stdout)
        while True:
            if run.cancel_event.is_set():
                cancelled = True
                process.kill()
                break
            if asyncio.get_event_loop().time() > deadline:
                process.kill()
                yield error(f"运行已超过 {settings.run_timeout_seconds} 秒上限，被强制终止")
                break
            try:
                line = await asyncio.wait_for(stdout_lines.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if stdout_lines.overflowed:
                stdout_lines.overflowed = False
                yield log(
                    f"内核单行输出超过 {_MAX_LINE_BYTES >> 20} MiB，该行内容过长已丢弃",
                    "warn",
                )
            if line is None:
                break
            for event in agent_run.feed(line.decode("utf-8", "replace").rstrip("\n")):
                yield scrub_event(event, _run_secrets(ctx))

        for event in agent_run.finish():
            yield scrub_event(event, _run_secrets(ctx))
        await process.wait()
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(stderr_task, timeout=2)
        stderr_text = "".join(stderr_chunks).strip()
        shutil.rmtree(run_dir, ignore_errors=True)

    if stderr_text and process.returncode not in (0, None):
        yield scrub_event(log(stderr_text[-4000:], "stderr"), _run_secrets(ctx))

    if cancelled or run.cancel_event.is_set():
        yield run_finished("cancelled", exit_code=process.returncode)
    elif process.returncode == 0:
        yield run_finished("ok", exit_code=0)
    else:
        if stderr_text and not agent_run.emitted_text:
            yield scrub_event(error(stderr_text[-800:]), _run_secrets(ctx))
        yield scrub_event(
            run_finished(
                "error",
                exit_code=process.returncode,
                detail=stderr_text[-800:] if stderr_text else None,
            ),
            _run_secrets(ctx),
        )


def _redact(argv: list[str], env: dict[str, str], *extra_secrets: str | None) -> list[str]:
    secrets = {
        value
        for key, value in env.items()
        if value and any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    # 适配器可能把密钥直接拼进 argv（如 custom-cli 的 {api_key} 占位符），
    # 此时它不一定出现在 env 里，需要显式补上。
    secrets.update(value for value in extra_secrets if value)
    out = []
    for part in argv:
        masked = part
        for secret in secrets:
            if secret and secret in masked:
                masked = masked.replace(secret, "***")
        out.append(masked)
    return out
