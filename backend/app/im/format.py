"""出站文本的平台化：Markdown → Telegram HTML / 纯文本，以及按平台上限切分。

各平台渲染能力不同（Telegram 收 HTML、其它多收 Markdown 原文或纯文本），单条长度上限
也不同。切分要尽量在段落 / 代码块边界处断开，避免把一段代码拦腰截断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_CODE_FENCE = re.compile(r"^```(\S*)\s*$")
_CLOSE_FENCE = re.compile(r"^```\s*$")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BOLD_STAR = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_UNDER = re.compile(r"__([^_]+)__")
_ITALIC_STAR = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_ITALIC_UNDER = re.compile(r"(?<![A-Za-z0-9_])_([^_\n]+)_(?![A-Za-z0-9_])")
_STRIKE = re.compile(r"~~([^~]+)~~")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_html(line: str) -> str:
    """一行里的行内 Markdown → HTML；行内代码先摘出来，里面的符号保持字面。"""
    spans: list[str] = []

    def hold(match: re.Match[str]) -> str:
        spans.append(f"<code>{_escape_html(match.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    kept = _INLINE_CODE.sub(hold, line)
    html = _escape_html(kept)
    html = _LINK.sub(lambda m: f'<a href="{_escape_html(m.group(2))}">{m.group(1)}</a>', html)
    html = _BOLD_STAR.sub(r"<b>\1</b>", html)
    html = _BOLD_UNDER.sub(r"<b>\1</b>", html)
    html = _ITALIC_STAR.sub(r"<i>\1</i>", html)
    html = _ITALIC_UNDER.sub(r"<i>\1</i>", html)
    html = _STRIKE.sub(r"<s>\1</s>", html)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], html)


def _inline_plain(line: str) -> str:
    text = _INLINE_CODE.sub(r"\1", line)
    text = _LINK.sub(
        lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})",
        text,
    )
    text = _BOLD_STAR.sub(r"\1", text)
    text = _BOLD_UNDER.sub(r"\1", text)
    text = _ITALIC_STAR.sub(r"\1", text)
    text = _ITALIC_UNDER.sub(r"\1", text)
    return _STRIKE.sub(r"\1", text)


@dataclass(slots=True)
class Block:
    """切分后的一个块：``code`` 是围栏代码块，``text`` 是普通段落。"""

    kind: str
    lines: list[str] = field(default_factory=list)
    lang: str = ""

    @property
    def body(self) -> str:
        return "\n".join(self.lines)

    def html(self) -> str:
        """这一块的 HTML 形式（代码块带 ``<pre><code>``，普通段落逐行转行内标记）。"""
        if self.kind == "code":
            return f"{self.code_open()}{_escape_html(self.body)}</code></pre>"
        return "\n".join(_inline_html(line) for line in self.lines)

    def code_open(self) -> str:
        """代码块的起始标签（带语言 class），单块超限时按行切开要复用它。"""
        lang = f' class="language-{_escape_html(self.lang)}"' if self.lang else ""
        return f"<pre><code{lang}>"

    def text(self) -> str:
        """这一块的纯文本形式（回退发送时用）。"""
        if self.kind == "code":
            return self.body
        return "\n".join(_inline_plain(line) for line in self.lines)


def _blocks(markdown: str) -> list[Block]:
    """把一份 Markdown 切成块：围栏代码块自成一类，其余按空行分段。"""
    out: list[Block] = []
    lines = markdown.split("\n")
    at = 0
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            out.append(Block("text", list(buffer)))
            buffer.clear()

    while at < len(lines):
        line = lines[at]
        fence = _CODE_FENCE.match(line)
        if fence is not None:
            flush()
            close = next(
                (index for index, item in enumerate(lines[at + 1 :]) if _CLOSE_FENCE.match(item)),
                -1,
            )
            body = lines[at + 1 :] if close == -1 else lines[at + 1 : at + 1 + close]
            out.append(Block("code", list(body), fence.group(1) or ""))
            at += 1 + (len(body) if close == -1 else close + 1)
            continue
        if line.strip() == "":
            flush()
        else:
            buffer.append(line)
        at += 1
    flush()
    return out


def to_plain(markdown: str) -> str:
    return "\n".join(block.text() for block in _blocks(markdown))


def _without_blank_edges(text: str) -> str:
    """去掉首尾的空行：客户端会把它们显示成上方多一行 / 下方多一截空白。"""
    lines = text.split("\n")
    start, end = 0, len(lines)
    while start < end and lines[start].strip() == "":
        start += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    if start == end:
        return ""
    return "\n".join(lines[start:end])


def chunk_markdown(markdown: str, limit: int) -> list[str]:
    """按平台上限切分（优先在换行 / 空格处断，实在没有才硬切）。"""
    rest = _without_blank_edges(str(markdown or ""))
    if rest.strip() == "":
        return []
    chunks: list[str] = []
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        at = limit
        newline = rest.rfind("\n", 0, at)
        if newline > limit // 2:
            at = newline
        else:
            space = rest.rfind(" ", 0, at)
            if space > limit // 2:
                at = space
        chunks.append(rest[:at])
        rest = rest[at:].lstrip()
    return chunks


def chunk_telegram(markdown: str, limit: int = 4096) -> list[tuple[str, str]]:
    """切分并同时给出「HTML 版」与「纯文本版」，两者按块对齐（回退时用同一段文字）。"""
    chunks: list[tuple[str, str]] = []
    html = ""
    text = ""

    def flush() -> None:
        nonlocal html, text
        if html or text:
            chunks.append((html, text))
            html, text = "", ""

    for block in _blocks(markdown):
        block_html = block.html()
        block_text = block.text()

        candidate = block_html if html == "" else f"{html}\n{block_html}"
        if len(candidate) <= limit:
            html = candidate
            text = block_text if text == "" else f"{text}\n{block_text}"
            continue
        flush()
        if len(block_html) <= limit:
            html, text = block_html, block_text
            continue
        # 单块就超限：代码块按行切开，每段各自带围栏（HTML 里就是各自一个 <pre>）；
        # 普通段落退成纯文本切分（宁可丢格式也不能发不出去）。
        if block.kind == "code":
            room = max(1, limit - len(block_html) + len(block_text))
            for part in chunk_markdown(block_text, room):
                escaped = _escape_html(part)
                chunks.append((f"{block.code_open()}{escaped}</code></pre>", part))
            continue
        for part in chunk_markdown(block_text, max(1, limit - 16)):
            chunks.append((_escape_html(part), part))
    flush()
    return chunks


__all__ = ["Block", "chunk_markdown", "chunk_telegram", "to_plain"]
