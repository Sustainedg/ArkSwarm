#!/usr/bin/env python3
"""生成协作进度页的等距视角办公室背景图（纯标准库，不引入任何依赖）。

用法：python3 scripts/gen_office_bg.py
输出：frontend/public/office/iso-office.svg

为什么是 SVG 而不是位图：这张图要跟着鼠标滚轮放大（最大 2.4 倍），矢量图放大不糊，
体积也只有几十 KB。构图是一间 13×10 格的等距办公室：地板、两面墙（落地窗 + 白板）、
地毯、书柜、饮水机、绿植，以及**按工位坐标摆好的办公桌**——每张桌子都带 `data-desk`
序号与 `data-x` / `data-y`（桌面中心），热区就钉在这两个数上。

工位坐标的唯一来源是 `frontend/src/lib/collabOffice.ts` 的 `OFFICE_DESKS`，本脚本直接读它，
所以「桌子」和「热区」不会错位；两边不一致时 `backend/tests/test_frontend_assets.py` 会失败。

墙上的挂钟同理：**钟面**画在这里（圆心与半径读 `collabOffice.ts` 的 `OFFICE_CLOCK`），
**指针**由前端 `frontend/src/components/OfficeClock.tsx` 按真实时间画 —— 秒针一秒一跳。

等距投影参数（`TILE_W` / `TILE_H` / `ORIGIN_X` / `ORIGIN_Y`）与 `collabOffice.ts` 无关，
只影响这张底图好不好看；改这里不用改前端。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TS_SOURCE = REPO / "frontend" / "src" / "lib" / "collabOffice.ts"
OUT_FILE = REPO / "frontend" / "public" / "office" / "iso-office.svg"

# ---------- 画布与等距投影 ----------
CANVAS_W, CANVAS_H = 1600, 1000
TILE_W, TILE_H = 112.0, 56.0
ORIGIN_X, ORIGIN_Y = 720.0, 250.0
FLOOR_COLS, FLOOR_ROWS = 13, 10  # 地砖格数（0..COLS / 0..ROWS）
WALL_H = 168.0                   # 墙高

BG = "#0a0913"
FLOOR_A = "#1e1a36"
FLOOR_B = "#262144"
PLINTH = "#151230"
WALL = "#211d3d"
WALL_TOP = "#332e63"


def iso(ix: float, iy: float) -> tuple[float, float]:
    """地砖坐标 → 画布坐标。"""
    return (ORIGIN_X + (ix - iy) * TILE_W / 2, ORIGIN_Y + (ix + iy) * TILE_H / 2)


def unproject(x: float, y: float) -> tuple[float, float]:
    """画布坐标 → 地砖坐标（读工位坐标时用）。"""
    u = (x - ORIGIN_X) / (TILE_W / 2)
    v = (y - ORIGIN_Y) / (TILE_H / 2)
    return ((u + v) / 2, (v - u) / 2)


def pt(p: tuple[float, float]) -> str:
    return f"{p[0]:.1f},{p[1]:.1f}"


def polygon(points: list[tuple[float, float]], fill: str, opacity: float = 1.0, extra: str = "") -> str:
    op = f' opacity="{opacity}"' if opacity < 1 else ""
    return f'<polygon points="{" ".join(pt(p) for p in points)}" fill="{fill}"{op}{extra}/>'


def v_face(p1: tuple[float, float], p2: tuple[float, float], lift: float, height: float,
           fill: str, opacity: float = 1.0) -> str:
    """贴在竖直面上的一块矩形：底边是 p1→p2，先抬 lift 再向上长 height。"""
    return polygon(
        [
            (p1[0], p1[1] - lift),
            (p2[0], p2[1] - lift),
            (p2[0], p2[1] - lift - height),
            (p1[0], p1[1] - lift - height),
        ],
        fill,
        opacity,
    )


def box(cx: float, cy: float, w: float, d: float, h: float, top: str, right: str, left: str,
        z: float = 0.0) -> str:
    """立在 (cx,cy) 这块地上的等距长方体：底面离地 z、高 h；画顶面 + 朝向观众的右前/左前面。"""
    put = lambda p: (p[0], p[1] - z)  # noqa: E731
    back, rightc = put(iso(cx - w / 2, cy - d / 2)), put(iso(cx + w / 2, cy - d / 2))
    front, leftc = put(iso(cx + w / 2, cy + d / 2)), put(iso(cx - w / 2, cy + d / 2))
    up = lambda p: (p[0], p[1] - h)  # noqa: E731
    return "".join([
        polygon([leftc, front, up(front), up(leftc)], left),      # 左前面
        polygon([rightc, front, up(front), up(rightc)], right),   # 右前面
        polygon([up(back), up(rightc), up(front), up(leftc)], top),  # 顶面
    ])


def shadow(cx: float, cy: float, w: float, d: float, opacity: float = 0.34) -> str:
    """家具在地板上的落影。"""
    p = iso(cx, cy)
    return (
        f'<ellipse cx="{p[0]:.1f}" cy="{p[1] + 8:.1f}" rx="{(w + d) * TILE_W / 4:.1f}" '
        f'ry="{(w + d) * TILE_H / 6:.1f}" fill="#000" opacity="{opacity}"/>'
    )


def read_desks() -> list[tuple[float, float]]:
    """从 collabOffice.ts 读工位坐标（唯一来源）。"""
    text = TS_SOURCE.read_text(encoding="utf-8")
    block = re.search(r"export const OFFICE_DESKS[^=]*=\s*\[(.*?)\]", text, re.S)
    if not block:
        raise SystemExit("没读到 OFFICE_DESKS：collabOffice.ts 的写法变了？")
    pairs = re.findall(r"\{\s*x:\s*(-?\d+(?:\.\d+)?)\s*,\s*y:\s*(-?\d+(?:\.\d+)?)\s*\}", block.group(1))
    if not pairs:
        raise SystemExit("OFFICE_DESKS 里没有解析出坐标")
    return [(float(x), float(y)) for x, y in pairs]


def read_clock() -> tuple[float, float, float]:
    """从 collabOffice.ts 读挂钟的圆心与半径（唯一来源）。"""
    text = TS_SOURCE.read_text(encoding="utf-8")
    block = re.search(r"export const OFFICE_CLOCK[^=]*=\s*\{(.*?)\}", text, re.S)
    if not block:
        raise SystemExit("没读到 OFFICE_CLOCK：collabOffice.ts 的写法变了？")
    values = {
        key: float(value)
        for key, value in re.findall(r"\b(x|y|r)\s*:\s*(-?\d+(?:\.\d+)?)", block.group(1))
    }
    if set(values) != {"x", "y", "r"}:
        raise SystemExit(f"OFFICE_CLOCK 缺 x / y / r：{block.group(1)!r}")
    return values["x"], values["y"], values["r"]


def clock_face() -> str:
    """墙上的挂钟：**只画钟面**（挂钉 + 钟壳 + 表盘 + 12 个刻度）。

    指针不在这儿 —— 它要跟着**真实时间**走，由前端按系统时钟画（`components/OfficeClock.tsx`），
    圆心与半径就是这里读到的 `OFFICE_CLOCK`，所以钟面和指针永远对得上。
    """
    cx, cy, r = read_clock()
    out = [f'<g class="office-clock-face" data-x="{cx:.1f}" data-y="{cy:.1f}" data-r="{r:.1f}">']
    out.append(f'<circle cx="{cx:.1f}" cy="{cy - r - 5:.1f}" r="2.4" fill="#4b4590"/>')  # 挂钉
    out.append(
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="#2b2750" '
        'stroke="#4b4590" stroke-width="2"/>'
    )
    out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r - 3.5:.1f}" fill="#0f0d1e"/>')
    for k in range(12):  # 12 个刻度：3 / 6 / 9 / 12 点加长
        angle = math.radians(k * 30)
        major = k % 3 == 0
        inner, outer = r - (10 if major else 7), r - 4
        out.append(
            f'<line x1="{cx + math.sin(angle) * inner:.1f}" y1="{cy - math.cos(angle) * inner:.1f}" '
            f'x2="{cx + math.sin(angle) * outer:.1f}" y2="{cy - math.cos(angle) * outer:.1f}" '
            f'stroke="#5f589f" stroke-width="{2.4 if major else 1.4}" stroke-linecap="round"/>'
        )
    out.append("</g>")
    return "".join(out)


def floor_layer() -> str:
    out = ["<g id=\"floor\">"]
    # 地基：比地板略大一圈的厚板，让房间「立」在地面上
    skirt = 0.4
    base = [
        iso(-skirt, -skirt), iso(FLOOR_COLS + skirt, -skirt),
        iso(FLOOR_COLS + skirt, FLOOR_ROWS + skirt), iso(-skirt, FLOOR_ROWS + skirt),
    ]
    base_down = [(x, y + 40) for x, y in base]
    out.append(polygon([base[3], base[2], base_down[2], base_down[3]], "#0f0d1e"))
    out.append(polygon([base[1], base[2], base_down[2], base_down[1]], "#0b0a16"))
    out.append(polygon(base, PLINTH))
    out.append(polygon([iso(0, 0), iso(FLOOR_COLS, 0), iso(FLOOR_COLS, FLOOR_ROWS), iso(0, FLOOR_ROWS)], FLOOR_A))
    # 交错地砖
    for ix in range(FLOOR_COLS):
        for iy in range(FLOOR_ROWS):
            if (ix + iy) % 2:
                continue
            t = [iso(ix, iy), iso(ix + 1, iy), iso(ix + 1, iy + 1), iso(ix, iy + 1)]
            out.append(polygon(t, FLOOR_B, 0.8))
    # 网格线
    for ix in range(FLOOR_COLS + 1):
        a, b = iso(ix, 0), iso(ix, FLOOR_ROWS)
        out.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" stroke="#ffffff" stroke-opacity="0.035"/>')
    for iy in range(FLOOR_ROWS + 1):
        a, b = iso(0, iy), iso(FLOOR_COLS, iy)
        out.append(f'<line x1="{a[0]:.1f}" y1="{a[1]:.1f}" x2="{b[0]:.1f}" y2="{b[1]:.1f}" stroke="#ffffff" stroke-opacity="0.035"/>')
    # 地毯
    rug = [iso(0.9, 1.9), iso(10.4, 1.9), iso(10.4, 9.4), iso(0.9, 9.4)]
    rug_in = [iso(1.2, 2.2), iso(10.1, 2.2), iso(10.1, 9.1), iso(1.2, 9.1)]
    out.append(polygon(rug, "#332b6b", 0.9))
    out.append(polygon(rug_in, "#2c2559", 0.75))
    out.append("</g>")
    return "".join(out)


def wall_layer() -> str:
    """两面墙：左墙（沿 iy 方向）挂白板与时钟，右墙（沿 ix 方向）是落地窗。"""
    out = ['<g id="walls">']
    # 左墙：ix = 0，从 iy=0 到 iy=FLOOR_ROWS
    a, b = iso(0, 0), iso(0, FLOOR_ROWS)
    out.append(polygon([a, b, (b[0], b[1] - WALL_H), (a[0], a[1] - WALL_H)], WALL))
    out.append(polygon([a, b, (b[0] - 7, b[1] - WALL_H - 6), (a[0] - 7, a[1] - WALL_H - 6)], WALL_TOP, 0.6))
    # 右墙：iy = 0，从 ix=0 到 ix=FLOOR_COLS
    c, d = iso(0, 0), iso(FLOOR_COLS, 0)
    out.append(polygon([c, d, (d[0], d[1] - WALL_H), (c[0], c[1] - WALL_H)], WALL))
    out.append(polygon([c, d, (d[0] + 7, d[1] - WALL_H - 6), (c[0] + 7, c[1] - WALL_H - 6)], WALL_TOP, 0.6))

    def wall_rect(axis: str, start: float, end: float, bottom: float, height: float,
                  fill: str, opacity: float = 1.0) -> str:
        """在墙上画一块矩形：axis='x' 表示贴在右墙（iy=0），'y' 表示贴在左墙（ix=0）。"""
        p1 = iso(start, 0) if axis == "x" else iso(0, start)
        p2 = iso(end, 0) if axis == "x" else iso(0, end)
        return v_face(p1, p2, bottom, height, fill, opacity)

    # 踢脚线
    out.append(wall_rect("x", 0.0, FLOOR_COLS, 0, 9, "#151233", 0.85))
    out.append(wall_rect("y", 0.0, FLOOR_ROWS, 0, 9, "#151233", 0.85))

    # 落地窗：三扇玻璃 + 夜空与城市灯
    out.append(wall_rect("x", 1.2, 6.2, 26, 108, "#16283a"))
    out.append(wall_rect("x", 1.2, 6.2, 26, 108, "url(#windowGlass)", 0.95))
    out.append(wall_rect("x", 1.2, 6.2, 26, 5, "#3a3765"))
    for k in range(1, 3):
        sep = iso(1.2 + k * 1.667, 0)
        out.append(f'<line x1="{sep[0]:.1f}" y1="{sep[1] - 31:.1f}" x2="{sep[0]:.1f}" y2="{sep[1] - 134:.1f}" stroke="#0d1424" stroke-width="4"/>')
    for k in range(12):
        lx = iso(1.5 + (k % 6) * 0.82, 0)
        ly = lx[1] - 46 - (k % 4) * 22
        out.append(f'<circle cx="{lx[0]:.1f}" cy="{ly:.1f}" r="{2.6 + (k % 3)}" fill="#ffd980" opacity="0.72"/>')

    # 白板 + 便签
    out.append(wall_rect("y", 2.8, 6.8, 34, 62, "#3b3670"))
    out.append(wall_rect("y", 2.8, 6.8, 36, 58, "#cfcbe8", 0.82))
    out.append(wall_rect("y", 3.1, 6.5, 42, 40, "#f2f0ff", 0.45))
    for k, (start, w) in enumerate(((3.0, 1.0), (4.4, 1.5), (6.1, 1.1))):
        p1, p2 = iso(0, start), iso(0, start + w)
        y = 96 + k * 16
        out.append(f'<line x1="{p1[0] + 7:.1f}" y1="{p1[1] - y:.1f}" x2="{p2[0] - 7:.1f}" y2="{p2[1] - y:.1f}" stroke="#5b52a0" stroke-width="3" opacity="0.7"/>')
    # 便签墙（白板右侧）：彩色小方片
    for k, color in enumerate(("#fbbf24", "#6ee7d1", "#d94fd0", "#8f86ff")):
        p1 = iso(0, 8.0 + k * 0.34)
        out.append(v_face(p1, (p1[0] + 5, p1[1] + 3), 96, 24, color, 0.85))

    # 挂钟（钟面；指针由前端按真实时间画）
    out.append(clock_face())
    out.append("</g>")
    return "".join(out)


def plant(cx: float, cy: float) -> str:
    pot = box(cx, cy, 0.5, 0.5, 26, "#4a3a63", "#332749", "#281d3a")
    p = iso(cx, cy)
    leaves = "".join(
        f'<ellipse cx="{p[0] + dx:.1f}" cy="{p[1] - 34 + dy:.1f}" rx="16" ry="10" fill="{color}" '
        f'opacity="0.92" transform="rotate({rot} {p[0] + dx:.1f} {p[1] - 34 + dy:.1f})"/>'
        for dx, dy, rot, color in ((-15, 5, -30, "#2f8f62"), (15, 3, 28, "#37a874"),
                                   (0, -8, 0, "#45c98b"), (-3, 15, -10, "#2b7c56"),
                                   (6, -4, 12, "#3fbe80"))
    )
    return shadow(cx, cy, 0.6, 0.6, 0.3) + pot + leaves


def bookshelf(cx: float, cy: float) -> str:
    body = box(cx, cy, 1.6, 0.5, 112, "#2f2b56", "#221f42", "#1a1733")
    p_left, p_right = iso(cx - 0.8, cy + 0.25), iso(cx + 0.8, cy + 0.25)
    shelves = ""
    for k in range(3):
        lift = 20 + k * 32
        shelves += v_face(p_left, p_right, lift, 5, "#3b3670", 0.95)
        for b in range(5):
            bx = -0.66 + b * 0.3
            color = ("#d94fd0", "#6ee7d1", "#fbbf24", "#7b5cff", "#ef7a5a")[(k + b) % 5]
            shelves += v_face(iso(cx + bx, cy + 0.25), iso(cx + bx + 0.22, cy + 0.25),
                              lift + 5, 20 + (b % 3) * 3, color, 0.85)
    return shadow(cx, cy, 1.8, 0.6, 0.34) + body + shelves


def cooler(cx: float, cy: float) -> str:
    base = box(cx, cy, 0.6, 0.6, 46, "#33305e", "#262346", "#1d1b38")
    q1, q2 = iso(cx - 0.3, cy + 0.3), iso(cx + 0.3, cy + 0.3)
    tap = v_face(q1, q2, 18, 13, "#1a1730", 0.9) + v_face(q1, q2, 31, 4, "#6ee7d1", 0.85)
    bottle = box(cx, cy, 0.4, 0.4, 30, "#4fd1ff", "#2f9fd0", "#23789e", z=46)
    p = iso(cx, cy)
    neck = (
        f'<polygon points="{p[0] - 16:.1f},{p[1] - 76:.1f} {p[0]:.1f},{p[1] - 68:.1f} '
        f'{p[0] + 16:.1f},{p[1] - 76:.1f} {p[0]:.1f},{p[1] - 84:.1f}" fill="#8fe9ff" opacity="0.6"/>'
        f'<polygon points="{p[0] - 17:.1f},{p[1] - 76:.1f} {p[0]:.1f},{p[1] - 68:.1f} '
        f'{p[0]:.1f},{p[1] - 44:.1f} {p[0] - 17:.1f},{p[1] - 52:.1f}" fill="#4fd1ff" opacity="0.5"/>'
        f'<polygon points="{p[0] + 17:.1f},{p[1] - 76:.1f} {p[0]:.1f},{p[1] - 68:.1f} '
        f'{p[0]:.1f},{p[1] - 44:.1f} {p[0] + 17:.1f},{p[1] - 52:.1f}" fill="#2f9fd0" opacity="0.45"/>'
    )
    return shadow(cx, cy, 0.8, 0.8, 0.3) + base + tap + bottle + neck


def desk_group(index: int, x: float, y: float) -> str:
    """一张工位：桌子 + 显示器（屏幕上有代码）+ 键盘 + 杯子 + 转椅。热区钉在 (x, y) 这块地上。"""
    ix, iy = unproject(x, y)
    out = [f'<g class="office-desk" data-desk="{index}" data-x="{x:.1f}" data-y="{y:.1f}">']
    out.append(shadow(ix, iy, 1.3, 1.0, 0.38))
    leg = ("#2b2750", "#211e42", "#191634")
    desk_h, top_h = 30, 6
    for lx in (-0.5, 0.5):
        for ly in (-0.32, 0.32):
            out.append(box(ix + lx, iy + ly, 0.16, 0.16, desk_h, *leg))
    out.append(box(ix, iy, 1.2, 0.9, top_h, "#5b549c", "#37326a", "#2a2552", z=desk_h))  # 桌板
    # 显示器：立在桌面上，屏幕朝外（左前）
    mx, my = ix, iy - 0.16
    out.append(box(mx, my, 0.62, 0.1, 34, "#232a44", "#3a3760", "#141a2b", z=desk_h + top_h))
    b1, b2 = iso(mx - 0.28, my + 0.05), iso(mx + 0.28, my + 0.05)
    out.append(v_face(b1, b2, desk_h + top_h + 5, 24, "#0d1120"))
    span = b2[0] - b1[0]
    for k, (frac, color) in enumerate(((0.62, "#6ee7d1"), (0.86, "#d94fd0"), (0.45, "#6ee7d1"), (0.72, "#8f86ff"))):
        ly = b1[1] - desk_h - top_h - 8 - k * 6
        out.append(
            f'<line x1="{b1[0] + 5:.1f}" y1="{ly:.1f}" x2="{b1[0] + 5 + (span - 10) * frac:.1f}" '
            f'y2="{ly - 2.5:.1f}" stroke="{color}" stroke-width="3" stroke-linecap="round" opacity="0.9"/>'
        )
    out.append(box(ix + 0.06, iy + 0.24, 0.4, 0.18, 5, "#403a74", "#2b2750", "#221f42", z=desk_h + top_h))
    out.append(box(ix - 0.44, iy + 0.2, 0.17, 0.17, 13, "#c8527f", "#9c3d61", "#7a2c4b", z=desk_h + top_h))
    # 转椅：坐在桌子前面（屏幕对面）
    out.append(box(ix, iy + 0.74, 0.42, 0.42, 18, "#3a3670", "#2b2856", "#211e42"))
    out.append(box(ix, iy + 0.92, 0.42, 0.12, 26, "#443f80", "#332e63", "#27234c", z=10))
    out.append("</g>")
    return "".join(out)


def build() -> str:
    desks = read_desks()
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS_W} {CANVAS_H}" width="{CANVAS_W}" height="{CANVAS_H}" role="img" aria-label="等距视角的协作办公室">',
        "<defs>",
        '<radialGradient id="floorGlow" cx="50%" cy="42%" r="62%">'
        '<stop offset="0%" stop-color="#7b5cff" stop-opacity="0.26"/>'
        '<stop offset="60%" stop-color="#7b5cff" stop-opacity="0.07"/>'
        '<stop offset="100%" stop-color="#7b5cff" stop-opacity="0"/></radialGradient>',
        '<linearGradient id="windowGlass" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#0d1a2e"/><stop offset="55%" stop-color="#16283a"/>'
        '<stop offset="100%" stop-color="#2a3350"/></linearGradient>',
        '<linearGradient id="screen" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#16233a"/><stop offset="100%" stop-color="#0b1020"/></linearGradient>',
        '<radialGradient id="vignette" cx="50%" cy="46%" r="74%">'
        '<stop offset="52%" stop-color="#000" stop-opacity="0"/>'
        '<stop offset="100%" stop-color="#040309" stop-opacity="0.78"/></radialGradient>',
        "</defs>",
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="{BG}"/>',
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="url(#floorGlow)"/>',
        wall_layer(),
        floor_layer(),
    ]
    # 家具：按「由里到外」画，前面的自然盖住后面的
    props: list[tuple[float, str]] = []
    for cx, cy, kind in (
        (0.7, 3.0, "plant"),
        (5.5, 0.8, "plant"),
        (12.3, 9.0, "plant"),
        (11.8, 0.9, "cooler"),
        (0.95, 7.5, "shelf"),
    ):
        if kind == "shelf":
            props.append((cx + cy, bookshelf(cx, cy)))
        elif kind == "cooler":
            props.append((cx + cy, cooler(cx, cy)))
        else:
            props.append((cx + cy, plant(cx, cy)))
    for index, (x, y) in enumerate(desks):
        ix, iy = unproject(x, y)
        props.append((ix + iy + 0.5, desk_group(index, x, y)))
    for _, svg in sorted(props, key=lambda item: item[0]):
        parts.append(svg)
    parts.append(f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="url(#vignette)" pointer-events="none"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(build(), encoding="utf-8")
    print(f"已生成 {OUT_FILE.relative_to(REPO)}（{OUT_FILE.stat().st_size / 1024:.1f} KB）")


if __name__ == "__main__":
    main()
