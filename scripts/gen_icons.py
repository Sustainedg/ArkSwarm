#!/usr/bin/env python3
"""生成应用图标（纯标准库，不引入任何依赖）。

用法：python3 scripts/gen_icons.py
输出：frontend/public/icons/{icon-192,icon-512,icon-maskable-512,apple-touch-icon}.png 与 icon.svg

图形是「品牌渐变方块 + 三节点星座 + 上方轨道弧」：一颗主节点在上、两颗成员节点在下，
用向内收的弧线连成一张小网络，顶上再绕一道细弧 —— 一眼读出「一个中枢调度一群智能体」。
它和界面里的品牌标（`components/BrandMark.tsx`）用的是同一组坐标，改一处必须同步另一处。

小尺寸可读性是硬约束：主节点 72/512、成员节点 48/512，缩到 16px 仍是一张清楚的三角星座；
连线与轨道弧只作陪衬（不透明度 45%~55%），糊掉了也不影响辨识。

抗锯齿用「有向距离场」而不是 NxN 超采样：所有图形（圆角方块 / 圆 / 折线 / 圆弧描边）都能直接
算出到边界的距离，按 1px 过渡带混合即可。512 尺寸的渲染从分钟级降到秒级。
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "frontend" / "public" / "icons"

#: 品牌渐变：品红 → 紫（与 index.css 的 --brand / 品牌紫一致）
BRAND_START = (0xE0, 0x53, 0xD6)
BRAND_END = (0x6B, 0x4C, 0xF0)

#: 三个节点 (x, y, r)：上方是中枢，下方两颗是成员。
NODES: tuple[tuple[float, float, float], ...] = (
    (256.0, 214.0, 72.0),
    (170.0, 348.0, 48.0),
    (342.0, 348.0, 48.0),
)

#: 节点之间的连线：端点索引 + 向质心内收的比例（0 = 直线，越大越弯）。
EDGES: tuple[tuple[int, int, float], ...] = ((0, 1, 0.24), (0, 2, 0.24), (1, 2, 0.12))
EDGE_STROKE = 22.0

#: 顶上的轨道弧 (cx, cy, r, 线宽, 起始角, 结束角)：与中枢同心，两端垂到主节点两侧。
#: 角度按 SVG 习惯（0° = 右侧，顺时针增大），跨 180° 时取 168°→372°。
ARC: tuple[float, float, float, float, float, float] = (256.0, 214.0, 118.0, 16.0, 168.0, 372.0)

#: 连线与轨道弧的白色不透明度：都低于节点，缩小时先糊掉的是它们，不是主体。
EDGE_OPACITY = 0.45

#: 画布 512 下的几何常量（PNG 与 SVG 共用）
CANVAS = 512.0
CORNER_RATIO = 0.225
RING_INSET = CANVAS * 0.016
RING_WIDTH = CANVAS * 0.009


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _mix(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = _clamp01(t)
    return tuple(round(a + (b - a) * t) for a, b in zip(start, end))  # type: ignore[return-value]


def _smoothstep(value: float) -> float:
    t = _clamp01(value)
    return t * t * (3.0 - 2.0 * t)


def _coverage(distance: float, width: float = 1.0) -> float:
    """距离 → 覆盖度：边界上 0.5，向内 1、向外 0，过渡带宽 ``width`` 像素。"""
    return _clamp01(0.5 - distance / width)


def _rounded_distance(x: float, y: float, half: float, radius: float) -> float:
    """到圆角矩形边界的**有向**距离（负数在内部）。"""
    qx = abs(x) - (half - radius)
    qy = abs(y) - (half - radius)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius


def _segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else _clamp01(((px - ax) * dx + (py - ay) * dy) / length)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _edge_points(a: tuple[float, float, float], b: tuple[float, float, float], bend: float) -> list[tuple[float, float]]:
    """一条边：从 a 到 b 的二次贝塞尔，控制点向三点质心内收，形成内凹弧。"""
    cx = sum(node[0] for node in NODES) / 3
    cy = sum(node[1] for node in NODES) / 3
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    qx, qy = mx + (cx - mx) * bend, my + (cy - my) * bend
    points = []
    for step in range(13):
        t = step / 12
        u = 1 - t
        points.append((u * u * a[0] + 2 * u * t * qx + t * t * b[0],
                       u * u * a[1] + 2 * u * t * qy + t * t * b[1]))
    return points


#: 三条边的折线（构建期算一次，光栅化时复用）
EDGE_POLYLINES = [_edge_points(NODES[a], NODES[b], bend) for a, b, bend in EDGES]


def _surface(x: float, y: float, size: float) -> tuple[float, float, float]:
    """底色：对角渐变 + 左上柔光 + 右下压暗，让方块有「玻璃砖」的体积感。

    坐标一律是画布坐标（左上为原点，0..size），与逐像素循环保持同一套空间。
    """
    t = ((x / size) + (y / size)) / 2
    red, green, blue = (float(c) for c in _mix(BRAND_START, BRAND_END, t))
    glow = _smoothstep(1.0 - math.hypot(x - size * 0.22, y - size * 0.18) / (size * 0.95))
    red += (255 - red) * 0.20 * glow
    green += (255 - green) * 0.20 * glow
    blue += (255 - blue) * 0.20 * glow
    shade = _smoothstep(math.hypot(x - size * 0.8, y - size * 0.86) / (size * 0.9) - 0.35)
    red *= 1 - 0.18 * shade
    green *= 1 - 0.18 * shade
    blue *= 1 - 0.18 * shade
    return red, green, blue


def _render_rgba(size: int, *, rounded: bool, mark_scale: float) -> bytes:
    """按有向距离场逐像素渲染。

    ``mark_scale`` 把「星座」整体绕画布中心收进去：可遮罩图标 / iOS 图标要留出安全区，
    系统按自己的形状裁剪时才不会切到节点。
    """
    half = size / 2
    radius = CORNER_RATIO * size
    inset = RING_INSET / CANVAS * size
    ring_width = RING_WIDTH / CANVAS * size
    stroke = EDGE_STROKE / CANVAS * size * mark_scale

    def place(x: float, y: float) -> tuple[float, float]:
        """把 512 画布坐标映射到本次尺寸，并按 mark_scale 绕中心收放。"""
        return (half + (x / CANVAS * size - half) * mark_scale,
                half + (y / CANVAS * size - half) * mark_scale)

    nodes = [(place(x, y)[0], place(x, y)[1], r / CANVAS * size * mark_scale)
             for x, y, r in NODES]
    polylines = [[place(x, y) for x, y in line] for line in EDGE_POLYLINES]
    # 每条边的包围盒（外扩半个描边）：离得远的像素直接跳过，省掉大部分距离计算
    boxes = [
        (min(x for x, _ in line) - stroke, min(y for _, y in line) - stroke,
         max(x for x, _ in line) + stroke, max(y for _, y in line) + stroke)
        for line in polylines
    ]
    # 轨道弧：圆心 / 半径 / 半线宽 / 起止角（弧度），与 512 画布按同一套缩放
    arc_cx, arc_cy = place(ARC[0], ARC[1])
    arc_r = ARC[2] / CANVAS * size * mark_scale
    arc_half = ARC[3] / CANVAS * size * mark_scale / 2
    arc_start, arc_end = math.radians(ARC[4]), math.radians(ARC[5])
    arc_span = arc_end - arc_start

    body = bytearray()
    for j in range(size):
        body.append(0)  # PNG 行过滤器：None
        y = j + 0.5
        for i in range(size):
            x = i + 0.5

            if rounded:
                shape = _coverage(_rounded_distance(x - half, y - half, half, radius))
                if shape <= 0.0:
                    body += b"\x00\x00\x00\x00"
                    continue
            else:
                shape = 1.0

            red, green, blue = _surface(x, y, size)

            if rounded:
                # 内圈高光：只在「外圈内、内圈外」那条细带上
                ring_d = abs(_rounded_distance(
                    x - half, y - half, half - inset - ring_width / 2, radius - inset - ring_width / 2
                ))
                ring = _coverage(ring_d - ring_width / 2)
                if ring > 0.0:
                    red += (255 - red) * 0.16 * ring
                    green += (255 - green) * 0.16 * ring
                    blue += (255 - blue) * 0.16 * ring

            # 连线（比节点淡一档：缩到 16px 时主次仍然分得开）
            edge_distance = float("inf")
            for line, (x0, y0, x1, y1) in zip(polylines, boxes):
                if x < x0 or x > x1 or y < y0 or y > y1:
                    continue
                for (ax, ay), (bx, by) in zip(line, line[1:]):
                    edge_distance = min(edge_distance, _segment_distance(x, y, ax, ay, bx, by))
            if edge_distance < float("inf"):
                edge = _coverage(edge_distance - stroke / 2)
                if edge > 0.0:
                    red += (255 - red) * EDGE_OPACITY * edge
                    green += (255 - green) * EDGE_OPACITY * edge
                    blue += (255 - blue) * EDGE_OPACITY * edge

            # 顶上的轨道弧：同一道白色，只描边
            arc_distance = abs(math.hypot(x - arc_cx, y - arc_cy) - arc_r) - arc_half
            if arc_distance < 0.5:
                angle = math.atan2(y - arc_cy, x - arc_cx)
                # 归一到 [arc_start, arc_start + 2π) 再和跨度比，跨 0° 也不会漏画
                offset = (angle - arc_start) % (2 * math.pi)
                if offset <= arc_span:
                    arc = _coverage(arc_distance)
                    if arc > 0.0:
                        red += (255 - red) * EDGE_OPACITY * arc
                        green += (255 - green) * EDGE_OPACITY * arc
                        blue += (255 - blue) * EDGE_OPACITY * arc

            # 节点：实心白
            node = 0.0
            for nx, ny, nr in nodes:
                node = max(node, _coverage(math.hypot(x - nx, y - ny) - nr))
            if node > 0.0:
                red += (255 - red) * node
                green += (255 - green) * node
                blue += (255 - blue) * node

            body += bytes((
                round(min(255.0, max(0.0, red))),
                round(min(255.0, max(0.0, green))),
                round(min(255.0, max(0.0, blue))),
                round(255 * shape),
            ))
    return bytes(body)


def _write_png(path: Path, size: int, body: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    blob = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(body, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(blob)
    # 打印时尽量用相对仓库根的路径；从临时目录调用（测试里）就打印绝对路径。
    try:
        shown = path.relative_to(REPO_ROOT)
    except ValueError:
        shown = path
    print(f"→ {shown} ({size}x{size}, {len(blob)} 字节)")


def _scaled_edge_paths(mark_scale: float) -> str:
    """三条边的二次贝塞尔路径（SVG 版），与光栅化用的是同一组控制点。"""
    center = CANVAS / 2
    cx = sum(node[0] for node in NODES) / 3
    cy = sum(node[1] for node in NODES) / 3
    ox = center + (cx - center) * mark_scale
    oy = center + (cy - center) * mark_scale
    nodes = [(center + (x - center) * mark_scale, center + (y - center) * mark_scale)
             for x, y, _ in NODES]
    paths = []
    for a, b, bend in EDGES:
        start, end = nodes[a], nodes[b]
        mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        qx, qy = mx + (ox - mx) * bend, my + (oy - my) * bend
        paths.append(f"M{start[0]:.1f} {start[1]:.1f} Q{qx:.1f} {qy:.1f} {end[0]:.1f} {end[1]:.1f}")
    return " ".join(paths)


def _arc_path(mark_scale: float) -> str:
    """轨道弧的 SVG 路径：与光栅化同一条圆弧（起点 / 终点按角度取点）。"""
    center = CANVAS / 2
    cx = center + (ARC[0] - center) * mark_scale
    cy = center + (ARC[1] - center) * mark_scale
    r = ARC[2] * mark_scale
    start, end = math.radians(ARC[4]), math.radians(ARC[5])
    span = end - start
    x0, y0 = cx + r * math.cos(start), cy + r * math.sin(start)
    x1, y1 = cx + r * math.cos(end), cy + r * math.sin(end)
    large = 1 if span > math.pi else 0
    return f"M{x0:.1f} {y0:.1f} A{r:.1f} {r:.1f} 0 {large} 1 {x1:.1f} {y1:.1f}"


def build_svg(size: int = 512, mark_scale: float = 1.0) -> str:
    """矢量版图标：渐变方块 + 内圈高光 + 三节点星座 + 轨道弧（与 PNG 同款几何）。"""
    scale = size / CANVAS
    radius = CORNER_RATIO * size
    inset = RING_INSET * scale
    ring_width = RING_WIDTH * scale
    ring_radius = radius - inset - ring_width / 2
    ring_size = size - 2 * inset - ring_width
    center = size / 2
    nodes = [(center + (x / CANVAS * size - center) * mark_scale,
              center + (y / CANVAS * size - center) * mark_scale,
              r / CANVAS * size * mark_scale) for x, y, r in NODES]
    circles = "\n".join(
        f'  <circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="#ffffff" />' for x, y, r in nodes
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#e053d6" />
      <stop offset="1" stop-color="#6b4cf0" />
    </linearGradient>
    <radialGradient id="glow" cx="0.22" cy="0.18" r="0.85">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.22" />
      <stop offset="1" stop-color="#ffffff" stop-opacity="0" />
    </radialGradient>
    <radialGradient id="shade" cx="0.82" cy="0.88" r="0.8">
      <stop offset="0" stop-color="#0b0a12" stop-opacity="0.2" />
      <stop offset="1" stop-color="#0b0a12" stop-opacity="0" />
    </radialGradient>
  </defs>
  <rect width="{size}" height="{size}" rx="{radius:.1f}" ry="{radius:.1f}" fill="url(#brand)" />
  <rect width="{size}" height="{size}" rx="{radius:.1f}" ry="{radius:.1f}" fill="url(#glow)" />
  <rect width="{size}" height="{size}" rx="{radius:.1f}" ry="{radius:.1f}" fill="url(#shade)" />
  <rect x="{inset:.1f}" y="{inset:.1f}" width="{ring_size:.1f}" height="{ring_size:.1f}"
        rx="{ring_radius:.1f}" ry="{ring_radius:.1f}" fill="none" stroke="#ffffff" stroke-opacity="0.16"
        stroke-width="{ring_width:.1f}" />
  <path d="{_scaled_edge_paths(mark_scale)}" fill="none" stroke="#ffffff" stroke-opacity="{EDGE_OPACITY}"
        stroke-width="{EDGE_STROKE * scale * mark_scale:.1f}" stroke-linecap="round" />
  <path d="{_arc_path(mark_scale)}" fill="none" stroke="#ffffff" stroke-opacity="{EDGE_OPACITY}"
        stroke-width="{ARC[3] * scale * mark_scale:.1f}" stroke-linecap="round" />
{circles}
</svg>
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 普通图标（any）：圆角方块，适配 Android / 桌面浏览器标签
    _write_png(OUT_DIR / "icon-192.png", 192, _render_rgba(192, rounded=True, mark_scale=1.0))
    _write_png(OUT_DIR / "icon-512.png", 512, _render_rgba(512, rounded=True, mark_scale=1.0))
    # 可遮罩图标（maskable）：满幅 + 图形收进安全区（系统会裁成圆形 / 水滴形）
    _write_png(OUT_DIR / "icon-maskable-512.png", 512, _render_rgba(512, rounded=False, mark_scale=0.76))
    # iOS 主屏图标：不支持透明，用满幅版本（iOS 自己会倒角），图形略收一点留白
    _write_png(OUT_DIR / "apple-touch-icon.png", 180, _render_rgba(180, rounded=False, mark_scale=0.86))
    (OUT_DIR / "icon.svg").write_text(build_svg(), encoding="utf-8")
    print(f"→ {(OUT_DIR / 'icon.svg').relative_to(REPO_ROOT)} (矢量)")


if __name__ == "__main__":
    main()
