"""给进程内（API 型）内核智能体使用的最小沙箱工具集。

CLI 型智能体自带工具，纯 API 的模型没有，所以由内核提供一套受限工具。
所有路径都相对工作目录解析，模型无法用 `../` 逃出去。
"""
from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

from ...egress import proxy_env_for_subprocess

MAX_OUTPUT = 8000
DEFAULT_TIMEOUT = 120

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在工作目录内执行一条 shell 命令，返回合并后的输出。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的 shell 命令"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取工作目录下某个 UTF-8 文本文件的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "起始行号，从 1 开始，可省略"},
                    "max_lines": {"type": "integer", "description": "最多读取的行数，默认 400"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "在工作目录下创建或覆盖一个文本文件。",
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
            "description": "列出工作目录下某个子目录的条目。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "默认 '.'"}},
            },
        },
    },
]


class ToolError(Exception):
    pass


class WorkspaceTools:
    def __init__(
        self,
        root: str,
        allow_shell: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        env: dict[str, str] | None = None,
    ) -> None:
        self.root = Path(root or os.getcwd()).resolve()
        self.allow_shell = allow_shell
        self.timeout = timeout
        #: 额外注入给 shell 的环境变量（智能体凭据）；不写进任何回显。
        self.env = dict(env or {})

    # -- 辅助方法 ------------------------------------------------------------

    def _resolve(self, raw: str) -> Path:
        if not raw:
            raise ToolError("必须提供路径")
        candidate = (self.root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(f"路径越出了工作目录：{raw}")
        return candidate

    # -- 工具实现 ------------------------------------------------------------

    async def run_shell(self, command: str) -> str:
        if not self.allow_shell:
            raise ToolError("该智能体已禁用 shell 访问")
        if not command:
            raise ToolError("必须提供命令")
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.root),
            env={**os.environ, **proxy_env_for_subprocess(), **self.env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ToolError(f"命令执行超过 {self.timeout} 秒，已被终止") from None
        output = stdout.decode("utf-8", "replace")
        return _truncate(f"$ {command}\n{output}\n[退出码 {proc.returncode}]")

    async def read_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"不是文件：{path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(start_line or 1))
        chunk = lines[start - 1 : start - 1 + max(1, int(max_lines or 400))]
        numbered = "\n".join(f"{start + i:>5} | {line}" for i, line in enumerate(chunk))
        return _truncate(numbered or "（空文件）")

    async def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or "", encoding="utf-8")
        return f"已写入 {path}（{len(content or '')} 字节）"

    async def list_dir(self, path: str = ".") -> str:
        target = self._resolve(path or ".")
        if not target.is_dir():
            raise ToolError(f"不是目录：{path}")
        rows = []
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            rows.append(f"{'目录' if entry.is_dir() else '文件'}  {entry.name}")
        return _truncate("\n".join(rows) or "（空目录）")

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        handler = getattr(self, name, None)
        if handler is None or name not in {s["function"]["name"] for s in TOOL_SPECS}:
            raise ToolError(f"未知工具：{name}")
        return await handler(**arguments)


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断 {len(text) - limit} 个字符）"


def shell_quote(value: str) -> str:
    return shlex.quote(value)
