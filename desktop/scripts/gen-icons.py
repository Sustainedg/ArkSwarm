#!/usr/bin/env python3
"""生成桌面壳图标：应用图标（icns / ico / png）与托盘图标。

图形与网页图标是**同一套几何**：直接复用主项目 ``scripts/gen_icons.py`` 里的渲染器
（三节点星座 + 轨道弧），改品牌图形时两边一起变，不会出现「桌面端图标和网页不一样」。

产物：
  build/icon.icns                macOS 应用图标（16→1024，内嵌 PNG）
  build/icon.ico                 Windows 应用图标（16→256，PNG 压缩条目）
  build/icon.png                 Linux / Electron 窗口图标（512）
  resources/icons/trayTemplate.png (+ @2x)   macOS 托盘：模板图（纯黑 + alpha，系统自动适配明暗）
  resources/icons/tray.png       Windows / Linux 托盘：品牌色小图

用法：python3 desktop/scripts/gen-icons.py
"""
from __future__ import annotations

import importlib.util
import math
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent  # 仓库根
DESKTOP = ROOT / "desktop"
BUILD = DESKTOP / "build"
TRAY_DIR = DESKTOP / "resources" / "icons"

#: 复用网页图标生成器的几何与 PNG 编码（纯标准库，无第三方依赖）
_spec = importlib.util.spec_from_file_location("arkswarm_icons", ROOT / "scripts" / "gen_icons.py")
icons = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(icons)


def render_app(size: int) -> bytes:
    """应用图标：圆角渐变方块 + 白色星座（与 icon-512 同款）。"""
    return icons._render_rgba(size, rounded=True, mark_scale=1.0)


def render_tray(size: int, template: bool) -> bytes:
    """托盘图标：只画星座本身，透明背景。

    - macOS 模板图必须是**纯黑 + alpha**（系统按菜单栏明暗重新着色），
      名字带 ``Template`` 才会被当成模板；
    - Windows / Linux 用品牌色，深浅任务栏都看得见。
    """
    # 托盘图要留一点边：16px 下贴着边缘会糊
    scale = size * 0.92 / icons.CANVAS
    pad = size * 0.04
    nodes = [(x * scale + pad, y * scale + pad, r * scale) for x, y, r in icons.NODES]
    lines = [
        [(x * scale + pad, y * scale + pad) for x, y in line] for line in icons.EDGE_POLYLINES
    ]
    stroke = icons.EDGE_STROKE * scale
    arc_cx = icons.ARC[0] * scale + pad
    arc_cy = icons.ARC[1] * scale + pad
    arc_r = icons.ARC[2] * scale
    arc_half = icons.ARC[3] * scale / 2
    arc_start, arc_end = math.radians(icons.ARC[4]), math.radians(icons.ARC[5])
    detailed = size > 20  # 16px 只留节点，弧线在这个尺寸只会糊成一团
    color = (0, 0, 0) if template else (0xE0, 0x53, 0xD6)

    body = bytearray()
    for j in range(size):
        body.append(0)  # PNG 行过滤器：None
        y = j + 0.5
        for i in range(size):
            x = i + 0.5
            alpha = 0.0
            if detailed:
                for line in lines:
                    for (ax, ay), (bx, by) in zip(line, line[1:]):
                        dist = icons._segment_distance(x, y, ax, ay, bx, by) - stroke / 2
                        alpha = max(alpha, icons._coverage(dist) * icons.EDGE_OPACITY)
                arc_dist = abs(math.hypot(x - arc_cx, y - arc_cy) - arc_r) - arc_half
                if arc_dist < 0.5:
                    angle = math.atan2(y - arc_cy, x - arc_cx)
                    if (angle - arc_start) % (2 * math.pi) <= (arc_end - arc_start):
                        alpha = max(alpha, icons._coverage(arc_dist) * icons.EDGE_OPACITY)
            for nx, ny, nr in nodes:
                alpha = max(alpha, icons._coverage(math.hypot(x - nx, y - ny) - nr))
            body += bytes((*color, round(255 * alpha)))
    return bytes(body)


def png_bytes(size: int, body: bytes) -> bytes:
    """与主项目同款 PNG 编码（RGBA）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", icons.zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", icons.zlib.compress(body, 9))
        + chunk(b"IEND", b"")
    )


def write_png(path: Path, size: int, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(size, body))
    print(f"→ {path.relative_to(ROOT)} ({size}x{size})")


def write_ico(path: Path, entries: list[tuple[int, bytes]]) -> None:
    """PNG 压缩条目的 ICO（Vista+ 支持，现代 Windows 图标的标准做法）。"""
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    payload = b""
    for size, png in entries:
        dim = 0 if size >= 256 else size  # 0 表示 256
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        payload += png
    path.write_bytes(header + directory + payload)
    print(f"→ {path.relative_to(ROOT)} ({[s for s, _ in entries]})")


def write_icns(path: Path, entries: list[tuple[bytes, bytes]]) -> None:
    """PNG 条目的 ICNS（macOS 10.7+ 支持，electron-builder 也接受）。"""
    chunks = b""
    for kind, png in entries:
        chunks += kind + struct.pack(">I", len(png) + 8) + png
    path.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)
    print(f"→ {path.relative_to(ROOT)} ({len(entries)} sizes)")


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)

    # ---- 应用图标 ----
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    write_ico(BUILD / "icon.ico", [(s, png_bytes(s, render_app(s))) for s in ico_sizes])

    # ICNS 的 PNG 类型：icp4=16, icp5=32, icp6=64, ic07=128, ic08=256, ic09=512, ic10=1024
    icns_map = [
        (b"icp4", 16),
        (b"icp5", 32),
        (b"icp6", 64),
        (b"ic07", 128),
        (b"ic08", 256),
        (b"ic09", 512),
        (b"ic10", 1024),
    ]
    write_icns(
        BUILD / "icon.icns",
        [(kind, png_bytes(size, render_app(size))) for kind, size in icns_map],
    )
    write_png(BUILD / "icon.png", 512, render_app(512))

    # ---- 托盘图标（resources/icons，随 files 打包） ----
    write_png(TRAY_DIR / "trayTemplate.png", 16, render_tray(16, template=True))
    write_png(TRAY_DIR / "trayTemplate@2x.png", 32, render_tray(32, template=True))
    write_png(TRAY_DIR / "tray.png", 32, render_tray(32, template=False))
    write_png(TRAY_DIR / "tray@2x.png", 64, render_tray(64, template=False))
    # 窗口图标（Windows 任务栏 / Linux）：随 files 打进 asar，运行时用 iconsDir()/icon.png 取
    write_png(TRAY_DIR / "icon.png", 256, render_app(256))


if __name__ == "__main__":
    main()