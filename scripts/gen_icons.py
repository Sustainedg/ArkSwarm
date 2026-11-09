#!/usr/bin/env python3
"""生成移动端 PWA 图标（纯标准库，不引入任何依赖）。

用法：python3 scripts/gen_icons.py
输出：frontend/public/icons/{icon-192,icon-512,icon-maskable-512,apple-touch-icon}.png 与 icon.svg

图标是「品牌渐变圆角方块 + 四角星」：与桌面端侧边栏的 ✦ 标识同源，
保证安装到主屏后与产品视觉一致。
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "frontend" / "public" / "icons"

BRAND_START = (0xD9, 0x4F, 0xD0)  # 品牌品红
BRAND_END = (0x7B, 0x5C, 0xFF)  # 品牌紫

SAMPLES = 3  # 每像素 3x3 超采样，用于抗锯齿


def _mix(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = min(1.0, max(0.0, t))
    return tuple(round(a + (b - a) * t) for a, b in zip(start, end))  # type: ignore[return-value]


def _star_inside(x: float, y: float, k: float) -> bool:
    """四角星（星芒）：sqrt(|x|)+sqrt(|y|)<=k 围成的凹四边形星形。"""
    return math.sqrt(abs(x)) + math.sqrt(abs(y)) <= k


def _rounded_inside(x: float, y: float, half: float, radius: float) -> bool:
    qx = abs(x) - (half - radius)
    qy = abs(y) - (half - radius)
    dist = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - radius
    return dist <= 0.0


def _render_rgba(size: int, *, rounded: bool, star_scale: float) -> bytes:
    half = size / 2
    radius = 0.225 * size
    star_k = math.sqrt(star_scale * size)
    body = bytearray()
    step = 1.0 / SAMPLES
    for j in range(size):
        body.append(0)  # PNG 行过滤器：None
        for i in range(size):
            red = green = blue = 0
            covered = 0
            for sy in range(SAMPLES):
                py = j + (sy + 0.5) * step - half
                for sx in range(SAMPLES):
                    px = i + (sx + 0.5) * step - half
                    if rounded and not _rounded_inside(px, py, half, radius):
                        continue
                    covered += 1
                    if _star_inside(px, py, star_k):
                        red += 255
                        green += 255
                        blue += 255
                    else:
                        cr, cg, cb = _mix(BRAND_START, BRAND_END, (px + py) / (2 * size) + 0.5)
                        red += cr
                        green += cg
                        blue += cb
            if covered == 0:
                body += b"\x00\x00\x00\x00"
                continue
            total = SAMPLES * SAMPLES
            alpha = round(255 * covered / total)
            average = (round(red / covered), round(green / covered), round(blue / covered))
            body += bytes((*average, alpha))
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
    print(f"→ {path.relative_to(path.parents[3])} ({size}x{size}, {len(blob)} 字节)")


def _write_svg(path: Path, size: int, star_scale: float) -> None:
    """矢量版图标：圆角方块 + 渐变 + 星芒多边形（与 PNG 同款几何）。"""
    half = size / 2
    radius = 0.225 * size
    star_r = star_scale * size
    points = []
    for step in range(72):
        theta = math.pi * 2 * step / 72
        # 星芒参数方程（星形线）：x=±R*cos^3, y=±R*sin^3
        x = star_r * math.copysign(abs(math.cos(theta)) ** 3, math.cos(theta))
        y = star_r * math.copysign(abs(math.sin(theta)) ** 3, math.sin(theta))
        points.append(f"{half + x:.2f},{half + y:.2f}")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">
  <defs>
    <linearGradient id="brand" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#d94fd0" />
      <stop offset="1" stop-color="#7b5cff" />
    </linearGradient>
  </defs>
  <rect width="{size}" height="{size}" rx="{radius:.1f}" ry="{radius:.1f}" fill="url(#brand)" />
  <polygon points="{' '.join(points)}" fill="#ffffff" />
</svg>
"""
    path.write_text(svg, encoding="utf-8")
    print(f"→ {path.relative_to(path.parents[3])} (矢量)")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 普通图标（any）：圆角方块，适配 Android / 桌面浏览器标签
    _write_png(OUT_DIR / "icon-192.png", 192, _render_rgba(192, rounded=True, star_scale=0.30))
    _write_png(OUT_DIR / "icon-512.png", 512, _render_rgba(512, rounded=True, star_scale=0.30))
    # 可遮罩图标（maskable）：满幅、留出安全区，避免被系统形状裁掉星芒
    maskable = _render_rgba(512, rounded=False, star_scale=0.26)
    _write_png(OUT_DIR / "icon-maskable-512.png", 512, maskable)
    # iOS 主屏图标：不支持透明，用满幅版本（iOS 自己会倒角）
    apple_touch = _render_rgba(180, rounded=False, star_scale=0.28)
    _write_png(OUT_DIR / "apple-touch-icon.png", 180, apple_touch)
    _write_svg(OUT_DIR / "icon.svg", 512, 0.30)


if __name__ == "__main__":
    main()
