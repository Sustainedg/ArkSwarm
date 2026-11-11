"""协作办公室底图的守卫测试。

等距办公室背景图（`frontend/public/office/iso-office.svg`）是**生成出来的**，不是手画的：
`scripts/gen_office_bg.py` 读 `frontend/src/lib/collabOffice.ts` 的 `OFFICE_DESKS` 去摆桌子，
前端的热区标记也钉在同一组坐标上。这里把这条约定固化下来：

- 提交进仓库的 SVG 必须和「现在的代码再跑一遍生成脚本」的结果一字不差；
- 每张桌子都要画出来，且落在房间地板范围内，热区才不会飘到墙外；
- 行布局（几个人一行）要和工位数对得上；
- 墙上的挂钟：底图只画钟面，圆心与半径必须和 `OFFICE_CLOCK` 一致（指针由前端按真实时间画）；
- 小人与工牌都钉在「椅子」上（不是桌心），并且跟着底图一起缩放 —— 底图那把转椅画在那儿。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
TS_SOURCE = REPO / "frontend" / "src" / "lib" / "collabOffice.ts"
SVG_FILE = REPO / "frontend" / "public" / "office" / "iso-office.svg"
GENERATOR = REPO / "scripts" / "gen_office_bg.py"

needs_frontend = pytest.mark.skipif(not TS_SOURCE.is_file(), reason="未检出前端源码目录")


def _read_ts() -> str:
    return TS_SOURCE.read_text(encoding="utf-8")


def _load_generator() -> ModuleType:
    """把生成脚本当模块导入（它自己会定位仓库根目录）。"""
    spec = importlib.util.spec_from_file_location("gen_office_bg", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_frontend
def test_committed_office_background_is_up_to_date():
    """底图必须是最新代码生成出来的——改了坐标却不重新生成，热区就会和桌子错位。"""
    module = _load_generator()
    assert SVG_FILE.is_file(), "缺少 frontend/public/office/iso-office.svg"
    assert SVG_FILE.read_text(encoding="utf-8") == module.build(), (
        "底图与 scripts/gen_office_bg.py 的产物不一致，请重新执行 python3 scripts/gen_office_bg.py"
    )


@needs_frontend
def test_every_hotspot_has_a_desk_on_the_floor():
    """每张桌子都画了，且桌心落在地板范围内。"""
    module = _load_generator()
    desks = module.read_desks()
    assert desks, "OFFICE_DESKS 没读到坐标"

    svg = SVG_FILE.read_text(encoding="utf-8")
    drawn = re.findall(r'class="office-desk" data-desk="(\d+)" data-x="([\d.]+)" data-y="([\d.]+)"', svg)
    assert len(drawn) == len(desks), "画出来的桌子数量和 OFFICE_DESKS 不一致"
    assert [int(index) for index, _, _ in drawn] == list(range(len(desks)))
    assert [(float(x), float(y)) for _, x, y in drawn] == desks, "SVG 里的工位坐标和 collabOffice.ts 对不上"

    for x, y in desks:
        ix, iy = module.unproject(x, y)
        assert 0 <= ix <= module.FLOOR_COLS, f"工位 {x},{y} 跑出地板（ix={ix:.2f}）"
        assert 0 <= iy <= module.FLOOR_ROWS, f"工位 {x},{y} 跑出地板（iy={iy:.2f}）"


@needs_frontend
def test_desk_rows_cover_every_desk():
    """行布局（第 0 行领导者一人、第 1 行 CTO 一人、往下每行一排）要把工位恰好分完。"""
    text = _read_ts()
    sizes = re.search(r"OFFICE_ROW_SIZES[^=]*=\s*\[([^\]]*)\]", text)
    assert sizes, "collabOffice.ts 缺少 OFFICE_ROW_SIZES"
    rows = [int(value) for value in re.findall(r"\d+", sizes.group(1))]
    assert len(rows) >= 2 and rows[0] == 1 and rows[1] == 1, "领导者与 CTO 应当各占一行、每行一人"
    assert sum(rows) == len(_load_generator().read_desks()), "OFFICE_ROW_SIZES 之和要等于工位数量"


@needs_frontend
def test_clock_face_matches_frontend_coordinates():
    """挂钟的钟面画在底图里、指针由前端按真实时间画，两者的圆心与半径必须是同一个。"""
    module = _load_generator()
    x, y, r = module.read_clock()
    svg = SVG_FILE.read_text(encoding="utf-8")
    drawn = re.search(
        r'class="office-clock-face" data-x="([\d.]+)" data-y="([\d.]+)" data-r="([\d.]+)"', svg
    )
    assert drawn, "底图里没找到挂钟（office-clock-face）"
    assert (float(drawn.group(1)), float(drawn.group(2)), float(drawn.group(3))) == (x, y, r), (
        "挂钟的圆心 / 半径和 collabOffice.ts 的 OFFICE_CLOCK 对不上"
    )
    assert r >= 16, "挂钟太小，指针根本看不清"
    assert r * 2 < module.WALL_H, "挂钟比墙还高"
    # 钟要挂在左墙上：左墙沿 iy 方向走，墙脚线可以按投影参数算出来。
    foot = module.ORIGIN_Y + (module.ORIGIN_X - x) * (module.TILE_H / module.TILE_W)
    assert foot - module.WALL_H < y < foot, "挂钟不在左墙的墙面上"
    left_edge = module.ORIGIN_X - module.TILE_W / 2 * module.FLOOR_ROWS
    assert left_edge < x < module.ORIGIN_X, "挂钟的横向位置不在左墙上"


@needs_frontend
def test_people_and_badges_are_pinned_to_the_chairs():
    """小人、工牌都钉在**椅子**上（不是桌心）：底图把转椅画在那儿，改错了人就会浮在桌面上。"""
    ts = _read_ts()
    assert "export const OFFICE_CHAIR_OFFSET" in ts, "collabOffice.ts 少了椅子相对桌心的偏移"
    assert "export function officeChairPoint(" in ts, "collabOffice.ts 少了 officeChairPoint()"

    owner = (REPO / "frontend" / "src" / "components" / "CollabOfficeMap.tsx").read_text(encoding="utf-8")
    assert owner.count("officeChairPoint(") >= 2, (
        "办公室地图里，小人（人那一层）和工牌都要用 officeChairPoint() 定位"
    )

    for css in ("frontend/src/index.css", "frontend/src/mobile/mobile.css"):
        text = (REPO / css).read_text(encoding="utf-8")
        assert ".office-worker > .collab-station-scene" in text, (
            f"{css} 缺少「小人跟着底图一起缩放」的规则（.office-worker > .collab-station-scene）"
        )
