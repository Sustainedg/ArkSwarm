#!/usr/bin/env python3
"""把内置头像包的白底批量抠成透明背景。

空会话的大头像直接显示在页面上（不再是圆框小图），白底在深色主题下像一块
补丁 —— 这里把 `public/avatars/pack-*.webp` 统一处理成透明版，**原地覆盖**
（文件名不变；缓存失效靠前端 `avatarPresetUrl` 的内容版本号，见
`lib/agentAvatar.ts` 的 `AVATAR_PACK_REV`，重跑本脚本后记得 +1）。

抠图判据（v2，两次踩坑后定稿）：

* flood-fill 从四边灌入，只清「与画布边缘连通」的区域 —— 人物内部再浅的
  颜色（白衬衫、白胡子）不挨着边缘，永远不会被整片掏空；
* 候选像素要同时满足**两个条件**才算背景：
  1. 与相邻背景像素的颜色距离 ≤ 14（局部比对，抗背景的轻微渐变）；
  2. **无彩度**：RGB 三通道极差 ≤ 10 —— 背景是中性灰白，而人物身上的浅色
     像素再亮也带色偏（肤色的暖调、衣服的冷调）。
  只用条件 1（v1，容差 28）会把浅色人物大片吃掉（14 张丢 8%~36%）；
  只用亮度阈值更不行（背景亮度与人物浅色部位重叠）。两个条件一起，
  实测 60 张全部零损失（人物覆盖率与白底原版一致）。

只依赖 Pillow。幂等：已透明的图重跑不变（但仍建议从 git 原版重跑，保证口径统一）。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
AVATAR_DIR = REPO_ROOT / "frontend" / "public" / "avatars"

#: 与相邻背景像素的颜色距离容差（RGB 欧氏距离）。
BG_TOLERANCE = 14
#: 背景的无彩度上限（RGB 极差）：灰白背景 ≤10，人物浅色带色偏 >10。
BG_CHROMA_MAX = 10


def flood_transparent(image: Image.Image) -> Image.Image:
    """从四边 flood-fill 白底并抠成透明（就地修改并返回 RGBA）。"""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    px = rgba.load()
    seen = [[False] * width for _ in range(height)]
    queue: deque[tuple[int, int, tuple[int, int, int]]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            queue.append((x, y, px[x, y][:3]))
    for y in range(height):
        for x in (0, width - 1):
            queue.append((x, y, px[x, y][:3]))
    while queue:
        x, y, ref = queue.popleft()
        if not (0 <= x < width and 0 <= y < height) or seen[y][x]:
            continue
        seen[y][x] = True
        r, g, b = px[x, y][:3]
        distance = ((r - ref[0]) ** 2 + (g - ref[1]) ** 2 + (b - ref[2]) ** 2) ** 0.5
        chroma = max(r, g, b) - min(r, g, b)
        if distance <= BG_TOLERANCE and chroma <= BG_CHROMA_MAX:
            px[x, y] = (r, g, b, 0)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and not seen[ny][nx]:
                    queue.append((nx, ny, (r, g, b)))
    return rgba


def main() -> None:
    avatars = sorted(AVATAR_DIR.glob("pack-*.webp"))
    if not avatars:
        raise SystemExit(f"没有找到头像包：{AVATAR_DIR}")
    for path in avatars:
        source = Image.open(path).convert("RGBA")
        # 已经透明过的图（四角全透明）不重复写，重跑保持幂等。
        alpha = source.getchannel("A")
        if all(
            alpha.getpixel(p) == 0
            for p in [
                (2, 2),
                (source.width - 3, 2),
                (2, source.height - 3),
                (source.width - 3, source.height - 3),
            ]
        ):
            continue
        flood_transparent(source).save(path, "WEBP", quality=92, method=6)
        print(f"  {path.name} → 透明背景 ✓")
    print(f"完成：{len(avatars)} 张头像已处理")


if __name__ == "__main__":
    main()
