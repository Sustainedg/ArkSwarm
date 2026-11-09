"""Execution layer: turns an :class:`Invocation` or an API harness into events."""
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
from ..settings import get_settings
from .manager import ActiveRun


async def stream_run(
    definition: AgentDefinition,
    ctx: AgentContext,
    run: ActiveRun,
) -> AsyncIterator[AgentEvent]:
    """Dispatch to the CLI or API execution path."""
    yield run_started(run.id)
    if definition.runtime is Runtime.API:
        async for event in definition.stream(ctx):
            if run.cancel_event.is_set():
                break
            yield event
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
    except Exception as exc:  # noqa: BLE001 - surface config errors to the user
        yield error(f"could not build the command: {exc}")
        yield run_finished("error")
        return

    run_dir = Path(tempfile.mkdtemp(prefix=f"magenta-{run.id}-"))
    for relative, content in (invocation.files or {}).items():
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    cwd = invocation.cwd or ctx.workspace_dir or os.getcwd()
    Path(cwd).mkdir(parents=True, exist_ok=True)

    env = {**os.environ, **invocation.env}
    env = {k: v.replace("{{RUN_DIR}}", str(run_dir)) for k, v in env.items()}

    executable = shutil.which(invocation.argv[0]) if invocation.argv else None
    if executable is None:
        yield log(f"launching {invocation.argv[0]}", "info")
    preview = " ".join(_redact(invocation.argv, env))
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
        yield error(f"`{invocation.argv[0]}` is not installed or not on PATH")
        yield run_finished("error")
        return
    except PermissionError as exc:
        yield error(f"cannot execute `{invocation.argv[0]}`: {exc}")
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

    async def drain_stderr() -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            stderr_chunks.append(line.decode("utf-8", "replace"))

    stderr_task = asyncio.create_task(drain_stderr())
    settings = get_settings()
    deadline = asyncio.get_event_loop().time() + settings.run_timeout_seconds
    cancelled = False

    try:
        assert process.stdout is not None
        while True:
            if run.cancel_event.is_set():
                cancelled = True
                process.kill()
                break
            if asyncio.get_event_loop().time() > deadline:
                process.kill()
                yield error(f"run exceeded the {settings.run_timeout_seconds}s timeout")
                break
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            for event in agent_run.feed(line.decode("utf-8", "replace").rstrip("\n")):
                yield event

        for event in agent_run.finish():
            yield event
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
        yield log(stderr_text[-4000:], "stderr")

    if cancelled or run.cancel_event.is_set():
        yield run_finished("cancelled", exit_code=process.returncode)
    elif process.returncode == 0:
        yield run_finished("ok", exit_code=0)
    else:
        if stderr_text and not agent_run.emitted_text:
            yield error(stderr_text[-800:])
        yield run_finished(
            "error",
            exit_code=process.returncode,
            detail=stderr_text[-800:] if stderr_text else None,
        )


def _redact(argv: list[str], env: dict[str, str]) -> list[str]:
    secrets = {
        value
        for key, value in env.items()
        if value and any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    out = []
    for part in argv:
        masked = part
        for secret in secrets:
            if secret and secret in masked:
                masked = masked.replace(secret, "***")
        out.append(masked)
    return out
