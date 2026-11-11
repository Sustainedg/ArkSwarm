"""移动端（`/m/`）与 PWA 的守卫测试。

移动端是一条与桌面端**物理独立**的链接：入口 HTML、页面、样式表、PWA 资产各自独立，
后端只用一个 SPA 兜底按前缀分派。下面的断言把这条约定固化下来，避免被后续改动悄悄破坏。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.main import resolve_spa_target

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"
MOBILE_SRC = FRONTEND / "src" / "mobile"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")


def test_spa_deep_links_split_between_desktop_and_mobile(tmp_path: Path):
    """`/m/*` 一律回落到移动端入口；其余深链回落桌面端入口。"""
    (tmp_path / "index.html").write_text("<html>desktop</html>", encoding="utf-8")
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "index.html").write_text("<html>mobile</html>", encoding="utf-8")
    (tmp_path / "m" / "sw.js").write_text("// service worker", encoding="utf-8")
    (tmp_path / "icons").mkdir()
    (tmp_path / "icons" / "icon.svg").write_text("<svg/>", encoding="utf-8")

    desktop = tmp_path / "index.html"
    mobile = tmp_path / "m" / "index.html"

    assert resolve_spa_target(tmp_path, "") == desktop
    assert resolve_spa_target(tmp_path, "chat/abc") == desktop
    assert resolve_spa_target(tmp_path, "roles") == desktop

    assert resolve_spa_target(tmp_path, "m") == mobile
    assert resolve_spa_target(tmp_path, "m/") == mobile
    assert resolve_spa_target(tmp_path, "m/chat/abc") == mobile
    assert resolve_spa_target(tmp_path, "m/collab/runs/xyz") == mobile

    # 静态产物原样返回：Service Worker 与图标不能被当成 SPA 深链。
    assert resolve_spa_target(tmp_path, "m/sw.js") == tmp_path / "m" / "sw.js"
    assert resolve_spa_target(tmp_path, "icons/icon.svg") == tmp_path / "icons" / "icon.svg"


def test_spa_falls_back_to_desktop_when_mobile_entry_is_missing(tmp_path: Path):
    """没有构建出移动端入口时（例如旧版本的前端产物），不要 404。"""
    (tmp_path / "index.html").write_text("<html>desktop</html>", encoding="utf-8")
    assert resolve_spa_target(tmp_path, "m/chat") == tmp_path / "index.html"


@needs_frontend
def test_mobile_entry_is_physically_separate_from_desktop():
    """两条链接各有自己的 HTML 入口、页面、样式表，互不引用。"""
    desktop_html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    mobile_html = (FRONTEND / "m" / "index.html").read_text(encoding="utf-8")

    assert "/src/mobile/main.tsx" in mobile_html
    assert "/m/manifest.webmanifest" in mobile_html
    assert "/src/main.tsx" not in mobile_html
    assert "src/mobile" not in desktop_html

    assert (MOBILE_SRC / "main.tsx").is_file()
    assert (MOBILE_SRC / "MobileApp.tsx").is_file()
    assert (MOBILE_SRC / "mobile.css").is_file()
    # 移动端不加载桌面端的样式表与页面。
    assert "index.css" not in (MOBILE_SRC / "main.tsx").read_text(encoding="utf-8")
    assert list(MOBILE_SRC.glob("pages/*.tsx")), "移动端应当有自己的页面目录"


@needs_frontend
def test_mobile_pages_do_not_import_desktop_ui():
    """移动端只允许复用 `lib/` 与纯展示组件，不得引用桌面端的页面与样式表。"""
    desktop_ui = {(FRONTEND / "src" / "pages").resolve(), (FRONTEND / "src").resolve()}
    offenders: list[str] = []
    for path in sorted(MOBILE_SRC.rglob("*.tsx")):
        for spec in re.findall(r"from '([^']+)'", path.read_text(encoding="utf-8")):
            if not spec.startswith("."):
                continue
            target = (path.parent / spec).resolve()
            name = target.name + (target.suffix if target.suffix else "")
            if target.parent in desktop_ui and target.parent.name == "pages":
                offenders.append(f"{path.name} 引用了桌面端页面：{spec}")
            elif target.parent.name == "src" and name in {"App.tsx", "App.ts", "main.tsx", "index.css"}:
                offenders.append(f"{path.name} 引用了桌面端入口：{spec}")
    assert offenders == [], "\n".join(offenders)


#: 「协作办公室」那一段样式：移动端不加载 index.css，所以两端**各写一份**，语义必须一致。
OFFICE_STYLE_BANNER = (
    "/* ================= 协作办公室：等距底图 + 工位上的小人 / 工牌 / 会走的挂钟 ================= */"
)
#: 「工单看板」那一段卡片样式：同样两端各一份，逐字一致（从横幅到「协作办公室」横幅为止）。
BOARD_STYLE_BANNER = (
    "/* ================= 工单看板：工单卡 / 动作徽章 / 流转时间线 / 规则手风琴 ================= */"
)
MOBILE_OFFICE_TUNING = "/* ---------- 移动端：办公室画布矮一点"


def _office_style_block(path: Path, cut: str | None = None) -> str:
    """取出一份样式表里的「协作办公室」样式段（可选：在移动端微调段之前截断）。"""
    text = path.read_text(encoding="utf-8")
    start = text.find(OFFICE_STYLE_BANNER)
    assert start >= 0, f"{path.name} 里找不到「协作办公室」样式段（横幅注释被删了？）"
    if cut is not None:
        end = text.find(cut, start)
        assert end > start, f"{path.name} 里找不到移动端微调段"
        text = text[:end]
    return text[start:].rstrip("\n")


@needs_frontend
def test_office_styles_are_identical_on_both_ends():
    """办公室的样式两端各写一份（移动端不加载 index.css），两份必须逐字一致。

    改了一边忘了另一边，手机上就会走样：小人不在工位上、工牌错位、挂钟指针飞出去。
    移动端自己那几条微调规则写在这一段之后，不参与比对。
    """
    desktop = _office_style_block(FRONTEND / "src" / "index.css")
    mobile = _office_style_block(MOBILE_SRC / "mobile.css", MOBILE_OFFICE_TUNING)
    assert desktop == mobile, (
        "index.css 与 mobile.css 的协作办公室样式不一致：改了一边记得把另一边同步（两端各一份）"
    )


def _board_style_block(path: Path) -> str:
    """取出一份样式表里的「工单看板」原子段（截到「协作办公室」段之前）。"""
    text = path.read_text(encoding="utf-8")
    start = text.find(BOARD_STYLE_BANNER)
    assert start >= 0, f"{path.name} 里找不到「工单看板」样式段（横幅注释被删了？）"
    end = text.find(OFFICE_STYLE_BANNER, start)
    assert end > start, f"{path.name} 里「工单看板」段之后应当还有「协作办公室」段"
    return text[start:end].rstrip("\n")


@needs_frontend
def test_ticket_board_card_styles_are_identical_on_both_ends():
    """工单卡（徽章 / 流转卡片 / 手风琴）的样式两端各一份，必须逐字一致。

    只读节点越权时的红色高亮就在这段里：改了一边忘了另一边，手机上就看不见越权提示。
    两端各自的布局（桌面三栏、移动端单栏）写在这一段之外，不参与比对。
    """
    desktop = _board_style_block(FRONTEND / "src" / "index.css")
    mobile = _board_style_block(MOBILE_SRC / "mobile.css")
    assert desktop == mobile, (
        "index.css 与 mobile.css 的「工单看板」卡片样式不一致：改了一边记得把另一边同步"
    )
    # 越权高亮必须真的在共享段里，不然这条守卫等于没守。
    assert ".board-entry.violation" in desktop
    assert ".board-tag.tone-error" in desktop


@needs_frontend
def test_ticket_board_is_shared_between_ends():
    """工单看板：视图模型与卡片零件只有一份，两端只是外壳与交互不同。"""
    kit = (FRONTEND / "src" / "components" / "TicketBoardKit.tsx").read_text(encoding="utf-8")
    assert "lib/ticketBoard" in kit

    desktop_board = (FRONTEND / "src" / "components" / "TicketBoard.tsx").read_text(encoding="utf-8")
    mobile_board = (MOBILE_SRC / "pages" / "MobileTicketBoardPage.tsx").read_text(encoding="utf-8")
    for text in (desktop_board, mobile_board):
        assert "lib/ticketBoard" in text, "工单看板应当用共享视图模型"
        assert "TicketBoardKit" in text, "工单看板应当用共享卡片零件"
        # 徽章配色 / 工单状态词汇 / 越权判定不许各写一份。
        for symbol in ("const ACTION_STYLE", "const TICKET_STATE", "const TICKET_LABELS", "const DEFAULT_SCOPE_BY_DUTY"):
            assert symbol not in text, f"工单看板又抄了一份词汇表：{symbol}"

    # 移动端要真的能走到看板（这正是之前缺失的那条路）
    assert "MobileTicketBoardPage.tsx" in {path.name for path in (MOBILE_SRC / "pages").glob("*.tsx")}
    assert "/collab/runs/:runId/tickets" in (MOBILE_SRC / "MobileApp.tsx").read_text(encoding="utf-8")
    run_page = (MOBILE_SRC / "pages" / "MobileCollabRunPage.tsx").read_text(encoding="utf-8")
    assert "/tickets`" in run_page, "移动端运行页要有打开工单看板的入口"


@needs_frontend
def test_mobile_manifest_scopes_to_the_mobile_link():
    manifest_path = FRONTEND / "public" / "m" / "manifest.webmanifest"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["scope"] == "/m/"
    assert manifest["start_url"] == "/m/"
    assert manifest["display"] == "standalone"

    icons = manifest["icons"]
    sizes = {icon["sizes"] for icon in icons}
    purposes = {icon.get("purpose") for icon in icons}
    assert {"192x192", "512x512"} <= sizes
    assert "maskable" in purposes
    for icon in icons:
        assert (FRONTEND / "public" / icon["src"].lstrip("/")).is_file(), f"图标缺失：{icon['src']}"


@needs_frontend
def test_mobile_service_worker_never_caches_the_api():
    """接口（含 SSE）必须直连后端：Service Worker 里要有明确的放行分支。"""
    script = (FRONTEND / "public" / "m" / "sw.js").read_text(encoding="utf-8")
    assert "self.addEventListener('fetch'" in script
    assert "'/api/'" in script
    assert "navigator.serviceWorker" not in script  # 注册放在入口，不写在 SW 里


@needs_frontend
def test_mobile_collab_editor_is_form_based_not_a_canvas():
    """移动端也能编排协作组，只是不用桌面端的画布连线（手机上拖不准）。"""
    pages = {path.name for path in (MOBILE_SRC / "pages").glob("*.tsx")}
    assert "MobileCollabTeamPage.tsx" in pages, "移动端应当有一个协作组编排页"
    assert "/collab/teams/:teamId" in (MOBILE_SRC / "MobileApp.tsx").read_text(encoding="utf-8")

    desktop_editor = ("CollabCanvas", "CollabSettingsPanel", "NodeInspector")
    offenders = [
        f"{path.relative_to(MOBILE_SRC)} → {spec}"
        for path in sorted(MOBILE_SRC.rglob("*.tsx"))
        for spec in re.findall(r"from '([^']+)'", path.read_text(encoding="utf-8"))
        if any(name in spec for name in desktop_editor)
    ]
    assert offenders == [], f"移动端不该引用桌面端的编排画布：{offenders}"


@needs_frontend
def test_collab_draft_vocabulary_has_a_single_source():
    """协作编排的字段语义与默认值只有一份：两端都从 `lib/collabDraft.ts` 取。"""
    draft = (FRONTEND / "src" / "lib" / "collabDraft.ts").read_text(encoding="utf-8")
    for symbol in ("DUTIES", "EDGE_KINDS", "SCOPES", "ACTIONS", "TICKET_STATES", "DEFAULT_BUDGET", "clampBudget"):
        assert symbol in draft, f"lib/collabDraft.ts 缺少 {symbol}"

    mobile_page = (MOBILE_SRC / "pages" / "MobileCollabTeamPage.tsx").read_text(encoding="utf-8")
    assert "lib/collabDraft" in mobile_page
    # 工单状态的中文标签也只有一份（进度卡从共享词汇表取）。
    progress = (FRONTEND / "src" / "lib" / "collabProgress.ts").read_text(encoding="utf-8")
    assert "const TICKET_LABELS" not in progress, "collabProgress 又抄了一份工单状态词汇表"

    for name in ("CollabCanvas.tsx", "CollabSettingsPanel.tsx", "NodeInspector.tsx"):
        text = (FRONTEND / "src" / "components" / name).read_text(encoding="utf-8")
        assert "lib/collabDraft" in text
        for symbol in ("const DUTIES", "const EDGE_KINDS", "const SCOPES", "const ACTIONS"):
            assert symbol not in text, f"{name} 又复制了一份编排词汇表（{symbol}）"


@needs_frontend
def test_mobile_entry_locks_zoom_and_desktop_keeps_it():
    """移动端是「固定外壳」的应用形态，锁定缩放；桌面端保持可缩放（浏览器缩放是它的无障碍能力）。"""
    mobile_html = (FRONTEND / "m" / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<meta name="viewport" content="([^"]+)"', mobile_html)
    assert match, "移动端入口缺少 viewport"
    viewport = {part.strip() for part in match.group(1).split(",")}
    assert "maximum-scale=1.0" in viewport
    assert "user-scalable=no" in viewport

    # iOS Safari 会忽略 user-scalable=no，必须靠 touch-action 兜底（允许集里不含 pinch-zoom）。
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")
    assert "touch-action: pan-x pan-y" in css
    # 外壳本身不许锁住手势；只有「协作办公室画布」例外——它要自己接管拖动与缩放。
    locked = [
        selector.strip()
        for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)
        if "touch-action: none" in body
    ]
    assert all(".office-map" in selector for selector in locked), (
        f"只有协作办公室画布能锁触摸手势，这里是：{locked}"
    )

    # 锁定缩放后输入框必须 ≥ 16px：iOS 聚焦 font-size < 16px 的输入框会强制放大页面。
    for selector in (".input, .select, .textarea", ".m-composer textarea", ".input.mono"):
        block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert block, f"mobile.css 缺少 {selector} 样式"
        size = re.search(r"font-size:\s*(\d+(?:\.\d+)?)px", block.group(1))
        assert size and float(size.group(1)) >= 16, f"{selector} 字号须 ≥ 16px"

    # 桌面端不禁用缩放。
    desktop_html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert "user-scalable" not in desktop_html
    assert "maximum-scale" not in desktop_html


@needs_frontend
def test_mobile_shell_is_pinned_to_the_visible_viewport():
    """外壳必须钉在「可视视口」上，底栏才会真正贴到屏幕底部。

    真机上地址栏与底部工具栏会伸缩：`100vh` / `100dvh` 与 `height: 100%` 的父链高度
    经常对不齐，底栏要么浮起来留缝、要么被工具栏压住。四边贴 0 的固定定位跟的是可视视口。
    """
    css = (MOBILE_SRC / "mobile.css").read_text(encoding="utf-8")

    app_rule = re.search(r"\.m-app \{([^}]*)\}", css)
    assert app_rule, "mobile.css 缺少 .m-app 样式"
    rule = app_rule.group(1)
    assert "position: fixed" in rule
    for edge in ("top: 0", "right: 0", "bottom: 0", "left: 0"):
        assert edge in rule, f".m-app 需要 {edge}（四边贴住可视视口）"
    assert "100vh" not in rule and "100dvh" not in rule, "外壳不该再用视口高度单位定高"

    html_rule = re.search(r"html, body \{([^}]*)\}", css)
    assert html_rule and "overflow: hidden" in html_rule.group(1), "文档本身不应滚动"


@needs_frontend
def test_mobile_shell_has_a_self_service_refresh():
    """独立应用（已添加主屏）没有刷新按钮：卡在旧外壳时必须能自助清缓存重载。

    改外壳（入口 HTML / 样式表 / 构建产物）必须把 SW 的缓存名版本 +1，老客户端下次打开才会换掉；
    设置页的「更新到最新版本」是兜底出口。
    """
    sw = (FRONTEND / "public" / "m" / "sw.js").read_text(encoding="utf-8")
    for name in ("magenta-m-shell-v", "magenta-m-assets-v"):
        assert name in sw, f"sw.js 的缓存名要带版本号（{name}<n>）"

    pwa = (MOBILE_SRC / "pwa.ts").read_text(encoding="utf-8")
    assert "export async function refreshShell" in pwa
    assert "unregister()" in pwa and "caches.keys()" in pwa
    assert "location.reload()" in pwa

    settings = (MOBILE_SRC / "pages" / "MobileSettingsPage.tsx").read_text(encoding="utf-8")
    assert "refreshShell" in settings, "设置页要能触发自助刷新"
