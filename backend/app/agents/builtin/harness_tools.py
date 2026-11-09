"""A minimal, sandboxed tool set for in-process (API) harness agents.

The CLI agents bring their own tools; a pure-API model has none, so the harness
supplies a small workspace-confined kit. Everything is resolved against the
workspace root, so a model can never escape it with `../`.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

MAX_OUTPUT = 8000
DEFAULT_TIMEOUT = 120

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command inside the workspace and return combined output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "1-based, optional"},
                    "max_lines": {"type": "integer", "description": "default 400"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries of a directory relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "default '.'"}},
            },
        },
    },
]


class ToolError(Exception):
    pass


class WorkspaceTools:
    def __init__(self, root: str, allow_shell: bool = True, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.root = Path(root or os.getcwd()).resolve()
        self.allow_shell = allow_shell
        self.timeout = timeout

    # -- helpers -------------------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        if not raw:
            raise ToolError("path is required")
        candidate = (self.root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(f"path escapes the workspace: {raw}")
        return candidate

    # -- tools ---------------------------------------------------------------

    async def run_shell(self, command: str) -> str:
        if not self.allow_shell:
            raise ToolError("shell access is disabled for this agent")
        if not command:
            raise ToolError("command is required")
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ToolError(f"command timed out after {self.timeout}s") from None
        output = stdout.decode("utf-8", "replace")
        return _truncate(f"$ {command}\n{output}\n[exit {proc.returncode}]")

    async def read_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(start_line or 1))
        chunk = lines[start - 1 : start - 1 + max(1, int(max_lines or 400))]
        numbered = "\n".join(f"{start + i:>5} | {line}" for i, line in enumerate(chunk))
        return _truncate(numbered or "(empty file)")

    async def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
        return f"wrote {len(content or '')} bytes to {path}"

    async def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path or ".")
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        rows = []
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            rows.append(f"{'dir ' if entry.is_dir() else 'file'}  {entry.name}")
        return _truncate("\n".join(rows) or "(empty)")

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(self, name, None)
        if handler is None or name not in {s["function"]["name"] for s in TOOL_SPECS}:
            raise ToolError(f"unknown tool: {name}")
        return await handler(**arguments)


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated {len(text) - limit} chars)"


def shell_quote(value: str) -> str:
    return shlex.quote(value)
