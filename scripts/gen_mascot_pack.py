#!/usr/bin/env python3
"""给内置头像包的每个角色生成「打招呼摆手」动画帧（离线、批量、随仓库发布）。

思路与 `gen_avatar_pack.py`（头像包本身）一脉相承：**模型只在离线阶段用一次**，
运行时零模型依赖 —— 头像包是「模型生成拼图 → 脚本裁切入库」，形象是「按头像
图生图一张姿势网格 → 脚本裁成 8 帧」。

> 当前界面**不使用**动画帧（生图模型给不出像素级连续的帧序列，观感是幻灯片），
> 空会话已回退为「静态大头像 + 呼吸浮动」。本脚本仅作为离线实验工具保留，
> 想再试时按下面的流程生成即可；它不参与运行时，也不随服务启动。

流程（对 `frontend/public/avatars/pack-NNN.webp` 的每一张）：

1. 以头像为参考图，让生图网关画一张 **4×2 姿势网格**：同一人物、同一打光、
   浅灰底，八个连贯的摆手姿势（垂手 → 抬臂 → 高位外摆 → 内摆 → 再外摆 →
   中位 → 低位 → 垂手）。一张图 8 帧，比逐帧生成省 7/8 的调用。
   网关配置从环境变量读（`MASCOT_GATEWAY` / `MASCOT_MODEL` / `MASCOT_API_KEY`，
   与 image-gen MCP 的参数同源），也可以用 `--gateway/--model/--api-key` 传入。
2. 裁切：等分 4×2 后每格独立抠图（flood-fill 抗渐变背景）+ 最大连通块清噪点
   + 包围盒居中裁方 —— 八帧构图对齐（中心 / 高度一致），轮播不跳。
3. 输出 `frontend/public/mascot/pack/pack-NNN-f{1..8}.webp`（256px 透明 WebP）。

只依赖 Pillow（生图请求用标准库 urllib）。已生成的角色默认跳过（`--force`
重生成）；中断了直接重跑，断点续齐。

注意：脚本侧的抠图算法与 `scripts/make_avatars_transparent.py`（头像包透明化）同源，
改一处记得同步另一处。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
AVATAR_DIR = REPO_ROOT / "frontend" / "public" / "avatars"
OUT_DIR = REPO_ROOT / "frontend" / "public" / "mascot" / "pack"

#: 期望的姿势数（请求 8 帧）；网关实际排成几列几行以**投影探测**为准 ——
#: 实测同一句提示词，网关时而画 4×2、时而画 3×2，写死等分必然切出半身碎片。
EXPECTED_FRAMES = 8
#: 输出帧边长（px）：与通用吉祥物 / 后端生成管线一致。
FRAME_SIZE = 256
#: 包围盒外扩比例：给摆手的手臂留呼吸感。
PADDING = 1.12
#: flood-fill 局部颜色距离容差（与 backend 管线同值）。
BG_TOLERANCE = 6
#: 连通块统计的采样步长。
BLOB_SAMPLE = 4
#: 单次生图超时（秒）。
TIMEOUT = 300.0

#: 姿势网格的画面描述。三个关键约束（都来自实测踩坑）：
#: 1. 人物要**小**且每格留足空白 —— 人物画大会举手出格，裁出来缺一角（被截断）；
#: 2. 相邻姿势是**小增量**（插值感）—— 姿势跨度大播放起来是幻灯片不是动画；
#: 3. 写死 "left arm stays down" —— 否则网关自作主张画双手合十。
GRID_PROMPT = (
    "Sprite sheet of 8 panels in a 4x2 grid, every panel shows the SAME character "
    "as the reference image in 3D clay style chibi, FULL BODY small and centered in "
    "each panel with generous empty margin around the character, arms never touching "
    "panel edges, soft studio lighting, plain light gray background, consistent "
    "character design and consistent size across all panels. The 8 panels form ONE "
    "continuous smooth waving-hello motion, each panel being a SMALL increment from "
    "the previous, like video frames: "
    "1) both arms relaxed down; "
    "2) right arm slightly bent outward, left arm stays down; "
    "3) right forearm rising to chest level, left arm stays down; "
    "4) right arm half raised to shoulder height, left arm stays down; "
    "5) right arm raised high beside the head, palm open, left arm stays down; "
    "6) right hand waving outward, wrist tilted, left arm stays down; "
    "7) right hand waving inward, left arm stays down; "
    "8) right hand waving outward again, warm smile. "
    "No text, no labels, no borders between panels"
)

#: 单张图片的下载上限（网关回 URL 时）。
MAX_IMAGE_BYTES = 32 * 1024 * 1024


# --- 生图网关（OpenAI 兼容 /images/edits，与 image-gen MCP 同一套口径） ---------


def _api_base(gateway: str) -> str:
    base = gateway.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _generate_grid(reference: bytes, gateway: str, model: str, api_key: str) -> bytes:
    """按头像生成一张 4×2 姿势网格，返回图片字节。"""
    url = f"{_api_base(gateway)}/images/edits"
    payload = {
        "model": model,
        "prompt": GRID_PROMPT,
        "n": 1,
        "size": "1024x1024",
        "images": [{"image_url": "data:image/png;base64," + base64.b64encode(reference).decode()}],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"生图网关返回 HTTP {exc.code}：{detail or '无响应正文'}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"无法连接生图网关：{exc}") from exc

    for item in result.get("data") or []:
        b64 = str(item.get("b64_json") or "")
        if b64:
            return base64.b64decode(b64)
        image_url = str(item.get("url") or "")
        if image_url:
            with urllib.request.urlopen(
                urllib.request.Request(image_url, headers={"Accept": "image/*"}), timeout=TIMEOUT
            ) as response:
                data = response.read(MAX_IMAGE_BYTES + 1)
            if data:
                return data
    raise RuntimeError("生图网关没有返回图片")


# --- 帧 处理（与 scripts/make_avatars_transparent.py 同源的抠图算法） --------


def _color_distance(r: int, g: int, b: int, br: int, bg: int, bb: int) -> float:
    return ((r - br) ** 2 + (g - bg) ** 2 + (b - bb) ** 2) ** 0.5


def _flood_transparent(image: Image.Image) -> Image.Image:
    """从四边 flood-fill 背景抠成透明（局部参考色，抗渐变背景与抗锯齿边）。"""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    px = rgba.load()
    # seen 按行开（外层 height、内层 width），访问统一 seen[y][x]。
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
        if _color_distance(r, g, b, *ref) <= BG_TOLERANCE:
            px[x, y] = (r, g, b, 0)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and not seen[ny][nx]:
                    queue.append((nx, ny, (r, g, b)))
    return rgba


def _main_blob_bbox(rgba: Image.Image, sample: int = BLOB_SAMPLE) -> tuple[int, int, int, int]:
    """只保留人物所在的最大连通块（清生成噪点），返回包围盒。"""
    width, height = rgba.size
    px = rgba.load()
    sw, sh = width // sample, height // sample
    # 采样网格统一按 [sy][sx] 开，访问处保持一致（行列错位在非方形图上会越界）。
    solid = [[False] * sw for _ in range(sh)]
    for sy in range(sh):
        for sx in range(sw):
            x0, y0 = sx * sample, sy * sample
            opaque = total = 0
            for dy in range(sample):
                for dx in range(sample):
                    x, y = x0 + dx, y0 + dy
                    if x >= width or y >= height:
                        continue
                    total += 1
                    if px[x, y][3] > 200:
                        opaque += 1
            if total > 0 and opaque > total / 2:
                solid[sy][sx] = True
    visited = [[False] * sw for _ in range(sh)]
    best_size, best_box = 0, None
    for sy in range(sh):
        for sx in range(sw):
            if not solid[sy][sx] or visited[sy][sx]:
                continue
            size = 0
            min_x = min_y = 10**9
            max_x = max_y = -1
            wave: deque[tuple[int, int]] = deque([(sx, sy)])
            visited[sy][sx] = True
            while wave:
                cx, cy = wave.popleft()
                size += 1
                min_x, min_y = min(min_x, cx), min(min_y, cy)
                max_x, max_y = max(max_x, cx), max(max_y, cy)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < sw and 0 <= ny < sh and solid[ny][nx] and not visited[ny][nx]:
                        visited[ny][nx] = True
                        wave.append((nx, ny))
            if size > best_size:
                best_size, best_box = size, (min_x, min_y, max_x, max_y)
    if best_box is None:
        raise RuntimeError("格子内找不到人物")
    min_x, min_y, max_x, max_y = best_box
    return (min_x * sample, min_y * sample, (max_x + 1) * sample, (max_y + 1) * sample)


def _cell_to_frame(cell: Image.Image) -> Image.Image:
    """网格里的一格 → 透明、按人物包围盒居中裁方、统一边长的帧。"""
    rgba = _flood_transparent(cell)
    x0, y0, x1, y1 = _main_blob_bbox(rgba)
    # 包围盒外的残留（噪点 / 邻格伸进来的衣角）一并清掉。
    px = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            if not (x0 <= x < x1 and y0 <= y < y1) and px[x, y][3] > 0:
                px[x, y] = (px[x, y][0], px[x, y][1], px[x, y][2], 0)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(x1 - x0, y1 - y0) * PADDING / 2
    left, top = round(cx - half), round(cy - half)
    size = round(half * 2)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(rgba.crop((max(0, left), max(0, top), max(0, left) + size, max(0, top) + size)), (0, 0))
    return canvas.resize((FRAME_SIZE, FRAME_SIZE), Image.LANCZOS)


def _grid_cells(grid: Image.Image) -> list[tuple[int, int, int, int]]:
    """投影探测姿势网格的实际布局，返回每格的（左、上、右、下）。

    行方向数「整行几乎没人物像素」的空隙 → 行分隔；列方向同理 → 列分隔。
    网关的排版并不守约（提示词要 4×2，实际常画 3×2），写死等分会把一格
    人物切成两半 —— 探测出来的几行几列就是几行几列，帧数跟着弹性走。
    探测失败（空隙少于 1 行 1 列）时退回 1×1 整图。
    """
    gray = grid.convert("L")
    width, height = gray.size
    px = gray.load()
    # 人物像素阈值：背景是浅灰（180+），人物整体更深。
    row_counts = [sum(1 for x in range(width) if px[x, y] < 180) for y in range(height)]
    col_counts = [sum(1 for y in range(height) if px[x, y] < 180) for x in range(width)]

    def spans(counts: list[int]) -> list[tuple[int, int]]:
        """把一条投影切成「有内容」的区间（空隙 ≥ 6px 才算分隔）。"""
        result: list[tuple[int, int]] = []
        start = None
        for index, count in enumerate(counts):
            if count > 3 and start is None:
                start = index
            elif count <= 3 and start is not None:
                if index - start >= 24:  # 太窄的内容条当噪点丢掉
                    result.append((start, index))
                start = None
        if start is not None and len(counts) - start >= 24:
            result.append((start, len(counts)))
        return result

    row_spans = spans(row_counts)
    col_spans = spans(col_counts)
    if not row_spans or not col_spans:
        return [(0, 0, width, height)]
    # 每格内再留 12px 边距，防邻格衣角伸进来。
    margin = 12
    return [
        (x0 + margin, y0 + margin, x1 - margin, y1 - margin)
        for y0, y1 in row_spans
        for x0, x1 in col_spans
    ]


def _frames_from_grid(grid: Image.Image) -> list[Image.Image]:
    """一张姿势网格 → 逐帧抠图裁方，**人物中心统一对齐到画布中心**。

    网格里人物的位置并不总在格心（网关排版略飘），直接按各格自身包围盒裁方
    会让帧与帧的中心漂移 —— 轮播时人物左右晃。所以边长用全网格的人物高度
    基准统一，每帧以**人物包围盒中心**为轴摆到画布正中。
    """
    cells: list[tuple[Image.Image, int, int, int, int]] = []  # (抠好的格, x0, y0, x1, y1)
    for box in _grid_cells(grid):
        rgba = _flood_transparent(grid.crop(box))
        cells.append((rgba, *_main_blob_bbox(rgba)))
    if not cells:
        raise RuntimeError("姿势网格里找不到人物")

    # 边长基准 = 所有格里最高的人物 × 外扩（垂手帧往往最窄，不能用宽定边）。
    tallest = max(y1 - y0 for _, _, y0, _, y1 in cells)
    half = tallest * PADDING / 2
    size = round(half * 2)
    frames: list[Image.Image] = []
    for rgba, x0, y0, x1, y1 in cells:
        # 包围盒外的残留（噪点 / 邻格伸进来的衣角）清掉。
        px = rgba.load()
        for y in range(rgba.height):
            for x in range(rgba.width):
                if not (x0 <= x < x1 and y0 <= y < y1) and px[x, y][3] > 0:
                    px[x, y] = (px[x, y][0], px[x, y][1], px[x, y][2], 0)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        left, top = round(cx - half), round(cy - half)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        crop_box = (
            max(0, left),
            max(0, top),
            min(rgba.width, left + size),
            min(rgba.height, top + size),
        )
        paste_at = (max(0, -left), max(0, -top))
        canvas.paste(rgba.crop(crop_box), paste_at)
        frames.append(canvas.resize((FRAME_SIZE, FRAME_SIZE), Image.LANCZOS))
    return frames


# --- 主流程 -------------------------------------------------------------------


def build(avatar: Path, gateway: str, model: str, api_key: str, force: bool) -> bool:
    """给一张头像包头像生成整套摆手帧；成功返回 True。"""
    if not force:
        existing = sorted(OUT_DIR.glob(f"{avatar.stem}-f*.webp"))
        if existing:
            print(f"  {avatar.stem}: 已有 {len(existing)} 帧，跳过（--force 重生成）")
            return True
    else:
        for stale in OUT_DIR.glob(f"{avatar.stem}-f*.webp"):
            stale.unlink()

    reference = avatar.read_bytes()
    raw = _generate_grid(reference, gateway, model, api_key)
    grid = Image.open(io.BytesIO(raw)).convert("RGB")
    frames = _frames_from_grid(grid)
    # 至少要有「抬手-摆动-放下」的骨架（3 帧），少于此说明网格画砸了。
    if len(frames) < 3:
        raise RuntimeError(f"姿势网格只切出 {len(frames)} 帧（布局探测失败）")
    for index, frame in enumerate(frames, start=1):
        buffer = io.BytesIO()
        frame.save(buffer, "WEBP", quality=90, method=6)
        (OUT_DIR / f"{avatar.stem}-f{index}.webp").write_bytes(buffer.getvalue())
    print(f"  {avatar.stem}: 生成 {len(frames)} 帧 ✓")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="给内置头像包批量生成摆手动画帧")
    parser.add_argument("--gateway", default=os.environ.get("MASCOT_GATEWAY", ""))
    parser.add_argument("--model", default=os.environ.get("MASCOT_MODEL", ""))
    parser.add_argument("--api-key", default=os.environ.get("MASCOT_API_KEY", ""), dest="api_key")
    parser.add_argument("--only", help="只处理这些头像（编号列表，如 009,023）")
    parser.add_argument("--force", action="store_true", help="已有帧也重新生成")
    args = parser.parse_args()

    if not args.gateway or not args.model:
        raise SystemExit(
            "请提供生图网关：--gateway https://... --model 名字（或环境变量 "
            "MASCOT_GATEWAY / MASCOT_MODEL / MASCOT_API_KEY，与 image-gen MCP 同源）"
        )

    avatars = sorted(AVATAR_DIR.glob("pack-*.webp"))
    if args.only:
        wanted = {f"pack-{part.strip().lstrip('0') or '0'}" for part in args.only.split(",")}
        avatars = [a for a in avatars if a.stem.lstrip("pack-").lstrip("0") or True]
        # 编号匹配用数字比较，免得 009 / 9 对不上
        def match(a: Path) -> bool:
            try:
                return str(int(a.stem.split("-")[1])) in {str(int(w.split("-")[1])) for w in wanted}
            except (ValueError, IndexError):
                return False
        avatars = [a for a in avatars if match(a)]
    if not avatars:
        raise SystemExit("没有匹配的头像包（先跑 scripts/gen_avatar_pack.py）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"给 {len(avatars)} 个头像生成摆手动画帧 → {OUT_DIR}")
    failures: list[str] = []
    for avatar in avatars:
        # 网关限流友好：每个角色之间歇口气。
        for attempt in range(2):
            try:
                build(avatar, args.gateway, args.model, args.api_key, args.force)
                break
            except Exception as exc:  # noqa: BLE001 - 单个失败不拖垮整批
                if attempt == 0:
                    print(f"  {avatar.stem}: 失败（{exc}），10 秒后重试一次")
                    time.sleep(10)
                else:
                    print(f"  {avatar.stem}: 放弃 —— {exc}")
                    failures.append(avatar.stem)
    if failures:
        print(f"完成，但 {len(failures)} 个失败：{', '.join(failures)}（重跑本脚本可断点续齐）")
        sys.exit(1)
    print("全部完成 ✓")


if __name__ == "__main__":
    main()
