"""「系统架构」页面：文档渲染器的单元测试。"""
from __future__ import annotations

from app.api.architecture_doc import build_architecture_page, render_markdown


def test_render_markdown_handles_headings_tables_and_inline():
    html = render_markdown(
        "# 标题\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n- 项目 **粗体** 与 `代码`\n"
    )
    assert "<h1>标题</h1>" in html
    assert "<th>A</th>" in html and "<td>2</td>" in html
    assert "<li>项目 <strong>粗体</strong> 与 <code>代码</code></li>" in html


def test_render_markdown_joins_wrapped_lines_by_language():
    html = render_markdown("第一行中文\n第二行中文\n\nEnglish line one\nEnglish line two\n")
    assert "<p>第一行中文第二行中文</p>" in html  # 中文折行不插空格
    assert "<p>English line one English line two</p>" in html  # 英文折行补空格


def test_render_markdown_escapes_html_inside_code_fences():
    html = render_markdown("```\n<script>alert(1)</script>\n```\n")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_architecture_page_reads_the_real_document():
    page = build_architecture_page()
    assert page["source"] == "docs/architecture.md"
    html = page["html"]
    assert "<!doctype html>" in html
    assert "<h2>1. 分层的目标</h2>" in html
    assert "<table>" in html  # 事件协议 / 扩展点表格
    assert "<pre><code" in html  # 分层与运行时序图
