"""把 ``docs/architecture.md`` 渲染成一个自包含的 HTML 页面。

文档是唯一事实源：接口每次请求都重新读取并渲染，因此本地更新
``docs/architecture.md`` 后，系统里的「系统架构」会同步更新，无需重启或重新构建。

这里刻意只依赖标准库（不引入 markdown 依赖，见 AGENTS.md 铁律 12），因此实现了
一个覆盖文档实际用法的精简 Markdown 渲染器：标题、段落、围栏代码块、GFM 表格、
有序/无序列表、引用、分隔线，以及行内的代码、加粗、斜体与链接。
"""
from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 仓库根目录下的文档路径（backend/app/api/architecture_doc.py → 上溯 3 层到仓库根）。
DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "architecture.md"
DOC_RELATIVE = "docs/architecture.md"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s*```(.*)$")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
#: 需要与上一行「无缝拼接」的字符（CJK 汉字及全角标点）。
_CJK = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")


def _escape(text: str) -> str:
    return html_lib.escape(text, quote=False)


def _join_wrapped(lines: list[str]) -> str:
    """把硬换行的段落合成一行：中文之间不插空格，其余用空格分隔。"""
    out = ""
    for raw in lines:
        piece = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
        elif _CJK.search(out[-1]) and _CJK.search(piece[0]):
            out += piece
        else:
            out += " " + piece
    return out


def _render_inline(text: str) -> str:
    text = _escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noreferrer">\1</a>',
        text,
    )
    return text


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and _TABLE_SEP.match(lines[index + 1]) is not None
    )


def render_markdown(markdown: str) -> str:
    """把文档正文片段渲染成 HTML（不含 ``<html>`` 外壳）。"""
    lines = markdown.split("\n")
    out: list[str] = []
    para: list[str] = []
    index = 0
    total = len(lines)

    def flush_paragraph() -> None:
        if para:
            out.append(f"<p>{_render_inline(_join_wrapped(para))}</p>")
            para.clear()

    while index < total:
        line = lines[index]

        fence = _FENCE.match(line)
        if fence:
            flush_paragraph()
            language = fence.group(1).strip()
            index += 1
            code: list[str] = []
            while index < total and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            index += 1  # 跳过结尾的 ```
            cls = f' class="language-{_escape(language)}"' if language else ""
            body = _escape("\n".join(code))
            out.append(f"<pre><code{cls}>{body}</code></pre>")
            continue

        if _is_table_start(lines, index):
            flush_paragraph()
            header = _split_row(lines[index])
            index += 2
            rows: list[list[str]] = []
            while index < total and "|" in lines[index] and lines[index].strip():
                rows.append(_split_row(lines[index]))
                index += 1
            head = "".join(f"<th>{_render_inline(c)}</th>" for c in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_render_inline(c)}</td>" for c in row) + "</tr>" for row in rows
            )
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_render_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if _RULE.match(line):
            flush_paragraph()
            out.append("<hr/>")
            index += 1
            continue

        if line.lstrip().startswith(">"):
            flush_paragraph()
            quote: list[str] = []
            while index < total and lines[index].lstrip().startswith(">"):
                quote.append(lines[index].lstrip()[1:])
                index += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(quote))}</blockquote>")
            continue

        if _BULLET.match(line) or _ORDERED.match(line):
            flush_paragraph()
            ordered = _ORDERED.match(line) is not None
            items: list[list[str]] = []
            while index < total:
                current = lines[index]
                bullet = _ORDERED.match(current) or _BULLET.match(current)
                if bullet:
                    items.append([bullet.group(2)])
                    index += 1
                elif items and current.strip() and current[:1] in (" ", "\t"):
                    items[-1].append(current)  # 折行的续行
                    index += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            rendered = "".join(f"<li>{_render_inline(_join_wrapped(item))}</li>" for item in items)
            out.append(f"<{tag}>{rendered}</{tag}>")
            continue

        if not line.strip():
            flush_paragraph()
            index += 1
            continue

        para.append(line)
        index += 1

    flush_paragraph()
    return "\n".join(out)


_PAGE_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px 32px 60px; background: #0b0a12; color: #e7e5f2;
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif;
  line-height: 1.7; font-size: 14px;
}
main { max-width: 900px; margin: 0 auto; }
h1, h2, h3, h4 { line-height: 1.35; margin: 1.6em 0 0.6em; }
h1 { font-size: 24px; border-bottom: 1px solid #2b2844; padding-bottom: 10px; }
h2 { font-size: 19px; color: #d94fd0; }
h3 { font-size: 16px; }
p { margin: 0.7em 0; }
a { color: #7b5cff; }
code {
  font-family: ui-monospace, "SF Mono", Consolas, monospace; font-size: 12.5px;
  background: #17151f; border: 1px solid #2b2844; border-radius: 5px; padding: 1px 5px;
}
pre {
  background: #121019; border: 1px solid #2b2844; border-radius: 10px;
  padding: 14px 16px; overflow: auto;
}
pre code { background: none; border: none; padding: 0; font-size: 12.5px; line-height: 1.55; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 13px; }
th, td { border: 1px solid #2b2844; padding: 7px 11px; text-align: left; vertical-align: top; }
th { background: #17151f; }
ul, ol { padding-left: 22px; margin: 0.7em 0; }
li { margin: 0.3em 0; }
blockquote { margin: 0.8em 0; padding: 2px 14px; border-left: 3px solid #7b5cff; color: #a9a5c4; }
hr { border: none; border-top: 1px solid #2b2844; margin: 1.6em 0; }
.doc-meta { color: #6f6a8f; font-size: 12px; margin-bottom: 18px; }
"""


def _wrap_page(body_html: str, updated_at: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"/>"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>"
        "<title>系统架构 · Magenta Agents</title>"
        f"<style>{_PAGE_STYLE}</style></head><body><main>"
        f"<div class=\"doc-meta\">{_escape(DOC_RELATIVE)} · 更新于 {_escape(updated_at)}</div>"
        f"{body_html}</main></body></html>"
    )


def build_architecture_page() -> dict[str, Any]:
    """读取文档并渲染：返回展示用 HTML、文档路径与更新时间。"""
    text = DOC_PATH.read_text(encoding="utf-8")
    stat = DOC_PATH.stat()
    updated_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    body = render_markdown(text)
    return {"html": _wrap_page(body, updated_at), "source": DOC_RELATIVE, "updated_at": updated_at}
