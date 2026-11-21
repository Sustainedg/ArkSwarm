#!/usr/bin/env python3
"""生成 Windows 安装向导的品牌位图（NSIS MUI 资源）。

与 ``gen-icons.py`` 同款思路：图标直接复用主项目 ``scripts/gen_icons.py`` 的渲染器
（母版位图 ``assets/brand/logo.png`` + Pillow），所以安装向导上的徽标、应用图标、
网页图标是**同一张图**，改品牌图形时一起变。

产物（全部提交进 git，CI 不跑本脚本）：
  build/installerSidebar.bmp  164×314  「完成」页左侧竖图（MUI_WELCOMEFINISHPAGE_BITMAP）
  build/installerHeader.bmp   150×57   其余向导页右上角横幅（MUI_HEADERIMAGE_BITMAP）

两个尺寸是 NSIS MUI 的**硬约束**，不是随便定的：MUI 按像素原样贴图，尺寸不符会被裁切
或留白。位深必须是 24 位（BGR、无 alpha）—— NSIS 的 ``LoadImage`` 对 32 位 BMP 的
alpha 处理依赖系统版本，24 位是最稳的公共子集。

用法：python3 desktop/scripts/gen-installer-assets.py
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

#: 复用网页图标生成器的几何与抗锯齿工具（与 gen-icons.py 用的是同一个模块）
_spec = importlib.util.spec_from_file_location("coda_icons", ROOT / "scripts" / "gen_icons.py")
icons = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(icons)

#: MUI 的硬性尺寸（164×314 竖图 / 150×57 横幅），改这两个数就得同步改 NSIS 布局
SIDEBAR_SIZE = (164, 314)
HEADER_SIZE = (150, 57)

#: 品牌渐变（与 index.css 的 --brand、gen_icons.py 的 BRAND_* 一致）
BRAND_START = (0xE0, 0x53, 0xD6)  # 品红
BRAND_END = (0x6B, 0x4C, 0xF0)  # 品牌紫

#: 侧边栏背景：从品牌紫渐变到深空色，让白色徽标浮起来
SIDEBAR_TOP = (0x5B, 0x2E, 0xC8)
SIDEBAR_BOTTOM = (0x0D, 0x08, 0x22)


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _smoothstep(value: float) -> float:
    t = _clamp01(value)
    return t * t * (3.0 - 2.0 * t)


def _mix(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> tuple[float, float, float]:
    t = _clamp01(t)
    return (start[0] + (end[0] - start[0]) * t,
            start[1] + (end[1] - start[1]) * t,
            start[2] + (end[2] - start[2]) * t)


def _render_app_rgba(size: int) -> bytes:
    """应用图标（品牌母版缩放）的紧密 RGBA 字节流，可直接参与合成。"""
    return icons.render_icon(size)


class Canvas:
    """RGB 浮点画布：所有叠加都在浮点域做，最后一次性量化，避免反复取整发灰。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.pixels = [[(0.0, 0.0, 0.0)] * width for _ in range(height)]

    def blend(self, x: int, y: int, color: tuple[float, float, float], alpha: float) -> None:
        """按 alpha 把 color 混到 (x, y)。越界直接丢弃（贴图边缘不需要环绕）。"""
        if alpha <= 0.0 or not (0 <= x < self.width and 0 <= y < self.height):
            return
        a = _clamp01(alpha)
        red, green, blue = self.pixels[y][x]
        self.pixels[y][x] = (
            red + (color[0] - red) * a,
            green + (color[1] - green) * a,
            blue + (color[2] - blue) * a,
        )

    def stamp_icon(self, rgba: bytes, size: int, cx: int, cy: int) -> None:
        """把一个 RGBA 图标合成上来（``cx`` / ``cy`` 是图标中心）。

        图标的精确尺寸由 ``size`` 决定，落点允许半个像素的偏移由四舍五入吃掉。
        """
        left = round(cx - size / 2)
        top = round(cy - size / 2)
        for row in range(size):
            for col in range(size):
                index = (row * size + col) * 4
                alpha = rgba[index + 3] / 255.0
                if alpha <= 0.0:
                    continue
                color = (float(rgba[index]), float(rgba[index + 1]), float(rgba[index + 2]))
                self.blend(left + col, top + row, color, alpha)

    def to_rgb(self) -> list[list[tuple[int, int, int]]]:
        """量化到 0..255 的整数三元组（浮点域里累加，只在这里取一次整）。"""
        return [[(round(min(255.0, max(0.0, r))),
                  round(min(255.0, max(0.0, g))),
                  round(min(255.0, max(0.0, b))))
                 for (r, g, b) in row] for row in self.pixels]


def write_bmp(path: Path, width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> None:
    """24 位 BMP：BGR 顺序、自下而上、每行 4 字节对齐（NSIS MUI 位图的唯一稳妥格式）。"""
    row_bytes = width * 3
    padding = (-row_bytes) % 4
    stride = row_bytes + padding

    data = bytearray()
    for y in range(height - 1, -1, -1):  # BMP 的扫描线自下而上
        for x in range(width):
            red, green, blue = pixels[y][x]
            data += bytes((blue, green, red))
        data += b"\x00" * padding

    file_header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(data), 0, 0, 14 + 40)
    # biHeight 为正 = 自下而上；biBitCount 24；biSizeImage 可填 0 但填上更规范
    dib_header = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(data), 2835, 2835, 0, 0
    )
    path.write_bytes(file_header + dib_header + bytes(data))
    print(f"→ {path.relative_to(ROOT)} ({width}×{height}, 24 位, {len(data) + 54} 字节)")


def build_sidebar() -> Canvas:
    """欢迎页 / 完成页左侧竖图：深空渐变 + 顶部品红辉光 + 居中大徽标 + 底部装饰线。"""
    width, height = SIDEBAR_SIZE
    canvas = Canvas(width, height)

    for y in range(height):
        t = _smoothstep(y / (height - 1))
        base = _mix(SIDEBAR_TOP, SIDEBAR_BOTTOM, t)
        for x in range(width):
            red, green, blue = base

            # 顶部品红辉光：让「品牌色」从上方渗下来，而不是一整块死板的渐变
            glow = _smoothstep(1.0 - math.hypot(x - width * 0.5, y - height * 0.04) / (width * 1.15))
            red += (BRAND_START[0] - red) * 0.30 * glow
            green += (BRAND_START[1] - green) * 0.30 * glow
            blue += (BRAND_START[2] - blue) * 0.30 * glow

            # 细网格点阵：极淡（4%），只在近距离看得出质感，远看是纯色
            if x % 10 == 0 and y % 10 == 0:
                red += (255.0 - red) * 0.04
                green += (255.0 - green) * 0.04
                blue += (255.0 - blue) * 0.04

            canvas.pixels[y][x] = (red, green, blue)

    # 居中徽标：与应用图标同源，用户安装完看到的就是同一张图
    icon_size = 104
    canvas.stamp_icon(_render_app_rgba(icon_size), icon_size, width // 2, round(height * 0.40))

    # 底部装饰：一条细线 + 三个渐隐圆点，收住竖图下缘，避免「图没画完」的感觉
    line_y = height - 46
    for x in range(22, width - 22):
        edge = min(x - 22, width - 22 - x) / 26.0
        canvas.blend(x, line_y, (255.0, 255.0, 255.0), 0.16 * _clamp01(edge))

    for offset in (-14, 0, 14):
        dot_x, dot_y, dot_r = width // 2 + offset, line_y + 14, 1.6
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                distance = math.hypot(dx, dy) - dot_r
                canvas.blend(dot_x + dx, dot_y + dy, (255.0, 255.0, 255.0),
                             0.34 * _clamp01(0.5 - distance))

    return canvas


def build_header() -> Canvas:
    """其余向导页右上角横幅：白底 + 居中徽标。

    MUI 把这张图贴在**白色**页面的右上角，所以底色必须是纯白 —— 否则会看到一块色块边界。
    """
    width, height = HEADER_SIZE
    canvas = Canvas(width, height)
    for y in range(height):
        for x in range(width):
            canvas.pixels[y][x] = (255.0, 255.0, 255.0)

    # 徽标：42px 居中。MUI 把这张 150px 的图贴在最右、标题文字在它左边，
    # 图内居中 → 视觉上落在「标题文字」和「窗口右缘」之间，两边留白对称。
    icon_size = 42
    icon_cx = width // 2
    canvas.stamp_icon(_render_app_rgba(icon_size), icon_size, icon_cx, height // 2)

    # 徽标下方一道极淡的品牌渐变短线（品红 → 紫），收住图标底边。
    # 长度与图标同宽、1px 高：小尺寸下只是一条「落地线」，放大才看得出是品牌色。
    left, right = icon_cx - icon_size // 2, icon_cx + icon_size // 2
    line_y = height // 2 + icon_size // 2 + 2
    for x in range(left, right):
        progress = (x - left) / max(1, right - left - 1)
        color = _mix(BRAND_START, BRAND_END, progress)
        canvas.blend(x, line_y, color, 0.55)

    return canvas


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    write_bmp(BUILD / "installerSidebar.bmp", *SIDEBAR_SIZE, build_sidebar().to_rgb())
    write_bmp(BUILD / "installerHeader.bmp", *HEADER_SIZE, build_header().to_rgb())


if __name__ == "__main__":
    main()
