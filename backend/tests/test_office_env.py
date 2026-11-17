"""协作办公室「拟人化 v2」的守卫测试（工程口径见 `docs/architecture.md` §11.7）。

这一层把 v2 的约定固化成**机器可判**的检查，让「刷新后个性一致」「凑一起讨论」「状态保真」不靠肉眼：

1. `frontend/package.json` 的 `dependencies` 与今天完全相同（零新依赖，PRD 验收 4）；
2. `lib/collabEnv.ts` 源码里没有 `Math.random` / `Date.now` / `performance.now`，种子由员工 key 派生；
3. **种子冻结表**：Python 用同一套 FNV-1a 重算 `workerSeed(key)`，与冻结表逐一比对；
4. **调度冻结表**：Python 镜像 `envBehaviorFor` / `envScheduleFor`，断言
   `waiting` / `error` 恒为 `null`、出现 `discuss` 的 tick 上配对两人**同时**是 `discuss`、
   允许集合与 §11.3 矩阵逐格一致；
5. CSS 文本层：4 个 `.env-*` + 5 个 `.fidget-*` 选择器与新增关键帧都落在**两端共享块内**，
   `state-error` / `state-waiting` 不出现在任何 `.env-*` 组合选择器里（防止有人用 CSS 硬开）；
6. 共享块内 `prefers-reduced-motion` 段覆盖所有新增关键帧；
7. 新增关键帧只动 `transform` / `opacity`（扫块内属性名）。

**镜像边界（说清，免得误读成空断言）**：pytest 里跑不了 TypeScript，所以「像不像」由两部分共同保证 ——
算法本身在上面第 3/4 条用 Python 逐字重算（伪码口径来自 §11.2），
`collabEnv.ts` 的源码常量与算式另有第 2 条 + `test_env_source_freezes_the_documented_arithmetic`
把它们钉死：常量、`(seed >>> 3)`、`phase * 7`、FNV 两个魔数、矩阵每一格、微动作周期四档，
改一个数就红。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO / "frontend"
ENV_TS = FRONTEND / "src" / "lib" / "collabEnv.ts"
OFFICE_TS = FRONTEND / "src" / "lib" / "collabOffice.ts"
MAP_TSX = FRONTEND / "src" / "components" / "CollabOfficeMap.tsx"
WORKER_TSX = FRONTEND / "src" / "components" / "CollabWorker.tsx"
DESK_TSX = FRONTEND / "src" / "components" / "WorkerAtDesk.tsx"
INDEX_CSS = FRONTEND / "src" / "index.css"
MOBILE_CSS = FRONTEND / "src" / "mobile" / "mobile.css"
SW_JS = FRONTEND / "public" / "m" / "sw.js"

needs_frontend = pytest.mark.skipif(not FRONTEND.is_dir(), reason="未检出前端源码目录")

#: 两端共用的「协作办公室」样式段（与 `test_mobile_web.py` 用的是同一条横幅）。
OFFICE_STYLE_BANNER = (
    "/* ================= 协作办公室：等距底图 + 工位上的小人 / 工牌 / 会走的挂钟 ================= */"
)
#: 移动端专用微调段：共享段到它为止。
MOBILE_OFFICE_TUNING = "/* ---------- 移动端：办公室画布矮一点"

#: 零新依赖：这一点依赖必须一字不差（含版本号）。
FROZEN_DEPENDENCIES = {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-markdown": "10.1.0",
    "react-router-dom": "^6.28.0",
    "remark-gfm": "4.0.1",
}

#: 种子冻结表：FNV-1a 的产物。这里变一个数，说明 `workerSeed` 的口径被人动了。
FROZEN_WORKER_SEEDS = {
    "ceo": 259979150,
    "cto": 257169031,
    "sde": 3643074133,
    "qa": 1816937991,
    "user": 1618501362,
}

#: §11.3 的状态覆盖矩阵（与 `docs/architecture.md` 的表逐格一致）。
ENV_ALLOWED = {
    "pending": ["rest"],
    "active": ["rest", "walk", "discuss", "call"],
    "waiting": [],
    "error": [],
    "done": ["rest", "walk", "discuss", "call"],
}
#: 允许微动作的状态：矩阵最后一行的 pending ❌ / active ✅ / waiting ❌ / error ❌ / done ✅。
FIDGET_ALLOWED = ["active", "done"]

ENV_TICK_MS = 5000
ENV_CYCLE_TICKS = 24
ENV_AWAY_TICKS = 6
DISCUSS_TICKS = 4


# --- 纯函数镜像（口径来自 docs/architecture.md §11.2） ---------------------------------


def fnv1a32(text: str) -> int:
    """32 位 FNV-1a：`h = ((h ^ byte) * 0x01000193) & 0xffffffff`（逐 UTF-8 字节）。"""
    hash_value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        hash_value = ((hash_value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return hash_value


def worker_seed(key: str) -> int:
    return fnv1a32(key)


def env_behavior_for(seed: int, tick: int, allowed: list[str]) -> str | None:
    if not allowed:
        return None
    phase = (tick + (seed >> 3)) % ENV_CYCLE_TICKS
    if phase >= ENV_AWAY_TICKS:
        return None
    return allowed[(seed + phase * 7) % len(allowed)]


def fidget_profile(seed: int) -> dict:
    kinds = ["blink", "scratch", "pen", "leg", "glance"]
    cycles = [5200, 6300, 7400, 8500]
    cycle_ms = cycles[(seed >> 4) % len(cycles)]
    return {
        "kind": kinds[seed % len(kinds)],
        "cycleMs": cycle_ms,
        "delayMs": -(seed % cycle_ms),
    }


def _read_layout() -> tuple[list[int], list[tuple[float, float]], tuple[float, float]]:
    """从 `collabOffice.ts` 读工位布局（只读，不改 —— §11.4 明确不新增坐标）。"""
    text = OFFICE_TS.read_text(encoding="utf-8")
    sizes = re.search(r"OFFICE_ROW_SIZES[^=]*=\s*\[([^\]]*)\]", text)
    assert sizes, "collabOffice.ts 里找不到 OFFICE_ROW_SIZES"
    row_sizes = [int(item) for item in re.findall(r"\d+", sizes.group(1))]
    desks_block = re.search(r"export const OFFICE_DESKS[^=]*=\s*\[(.*?)\]", text, re.S)
    assert desks_block, "collabOffice.ts 里找不到 OFFICE_DESKS"
    desks = [
        (float(x), float(y))
        for x, y in re.findall(
            r"\{\s*x:\s*(-?\d+(?:\.\d+)?)\s*,\s*y:\s*(-?\d+(?:\.\d+)?)\s*\}", desks_block.group(1)
        )
    ]
    offset = re.search(
        r"OFFICE_CHAIR_OFFSET\s*=\s*\{\s*x:\s*(-?\d+(?:\.\d+)?)\s*,\s*y:\s*(-?\d+(?:\.\d+)?)\s*\}",
        text,
    )
    assert offset, "collabOffice.ts 里找不到 OFFICE_CHAIR_OFFSET"
    return row_sizes, desks, (float(offset.group(1)), float(offset.group(2)))


def _row_bands(row_sizes: list[int], desks: list[tuple[float, float]]):
    bands, cursor = [], 0
    for size in row_sizes:
        if cursor >= len(desks):
            break
        bands.append(desks[cursor:cursor + size])
        cursor += size
    if cursor < len(desks):
        bands.append(desks[cursor:])
    return bands


def _row_index(desk: tuple[float, float], bands) -> int:
    best, best_distance = 0, float("inf")
    for index, band in enumerate(bands):
        if not band:
            continue
        distance = abs(band[0][1] - desk[1])
        if distance < best_distance:
            best, best_distance = index, distance
    return best


def _discuss_pairs(seats: list[dict], bands) -> list[dict]:
    grouped: dict[int, list[dict]] = {}
    for seat in seats:
        grouped.setdefault(_row_index(seat["desk"], bands), []).append(seat)
    pairs: list[dict] = []
    for index in sorted(grouped):
        row = sorted(grouped[index], key=lambda seat: (seat["desk"][0], seat["key"]))
        for cursor in range(0, len(row) - 1, 2):
            keys = [row[cursor]["key"], row[cursor + 1]["key"]]
            pairs.append({"keys": keys, "seed": worker_seed("discuss:" + "+".join(keys))})
    return pairs


def _env_schedule(seats: list[dict], tick: int, reduced_motion: bool, bands) -> dict:
    """`envScheduleFor` 的 Python 镜像。"""

    def allowed_of(state: str) -> list[str]:
        allowed = ENV_ALLOWED[state]
        if reduced_motion:
            return [item for item in allowed if item in ("rest", "call")]
        return allowed

    schedule = {}
    for seat in seats:
        # `discuss` 不由个人决定：个人调度里去掉它，只有配对窗口才让两人同时进入讨论。
        personal = [item for item in allowed_of(seat["state"]) if item != "discuss"]
        schedule[seat["key"]] = {
            "env": env_behavior_for(worker_seed(seat["key"]), tick, personal),
            "fidget": fidget_profile(worker_seed(seat["key"]))["kind"]
            if seat["state"] in FIDGET_ALLOWED
            else None,
            "dx": 0.0,
            "dy": 0.0,
        }
    by_key = {seat["key"]: seat for seat in seats}
    for pair in _discuss_pairs(seats, bands):
        pair_phase = (tick + (pair["seed"] >> 3)) % ENV_CYCLE_TICKS
        if pair_phase >= DISCUSS_TICKS:
            continue
        members = [by_key[key] for key in pair["keys"]]
        if not all("discuss" in allowed_of(seat["state"]) for seat in members):
            continue
        chairs = [(seat["desk"][0] + 0, seat["desk"][1] + 0) for seat in members]
        # 聚点 = 两人椅点的中点；各留 ±3px 残差（面对面对谈，不完全重叠）。
        mid_x = (chairs[0][0] + chairs[1][0]) / 2
        mid_y = (chairs[0][1] + chairs[1][1]) / 2
        offsets = [
            (mid_x - chair[0] + (3 if index == 0 else -3), mid_y - chair[1])
            for index, chair in enumerate(chairs)
        ]
        for key, (dx, dy) in zip(pair["keys"], offsets):
            schedule[key] = {**schedule[key], "env": "discuss", "dx": dx, "dy": dy}

    chair_offset = _CHAIR_OFFSET
    walk_step = (chair_offset[0] / 0.74 * 1.6, chair_offset[1] / 0.74 * 1.6)
    for seat in seats:
        if schedule[seat["key"]]["env"] == "walk":
            schedule[seat["key"]]["dx"] = walk_step[0]
            schedule[seat["key"]]["dy"] = walk_step[1]
    return schedule


_ROW_SIZES, _DESKS, _CHAIR_OFFSET = _read_layout() if OFFICE_TS.is_file() else ([], [], (0.0, 0.0))
_BANDS = _row_bands(_ROW_SIZES, _DESKS)


def _seats(state: str = "active", count: int = len(_DESKS)) -> list[dict]:
    """按真实工位布局摆 `count` 个人（一排几人由 `OFFICE_ROW_SIZES` 决定）。"""
    keys = [f"agent-{index}" for index in range(count)]
    seats = []
    for index, key in enumerate(keys):
        desk = _DESKS[index % len(_DESKS)]
        lap = index // len(_DESKS)
        seats.append({"key": key, "state": state, "desk": (desk[0] + lap * 18, desk[1] + lap * 34)})
    return seats


# --- CSS 文本工具 ---------------------------------------------------------------------


def _shared_block(path: Path, banner: str, cut: str | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.find(banner)
    assert start >= 0, f"{path.name} 里找不到共享样式段：{banner[:24]}…"
    if cut is not None:
        end = text.find(cut, start)
        assert end > start, f"{path.name} 里共享段之后找不到 {cut[:24]}…"
        text = text[:end]
    return text[start:].rstrip("\n")


def _office_blocks() -> list[tuple[str, str]]:
    return [
        ("index.css", _shared_block(INDEX_CSS, OFFICE_STYLE_BANNER)),
        ("mobile.css", _shared_block(MOBILE_CSS, OFFICE_STYLE_BANNER, MOBILE_OFFICE_TUNING)),
    ]


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _rules(text: str, context: str = "") -> list[tuple[str, str, str]]:
    """把 CSS 拍平成 `(完整选择器, 声明块, 所在 @ 规则)` 列表。

    `@media` / `@keyframes` 会递归进它的内部规则，`context` 记下它属于哪个 @ 规则，
    这样既能把关键帧属性扫干净，也能单独挑出 `prefers-reduced-motion` 那一段。
    """
    text = _strip_comments(text)
    rules: list[tuple[str, str, str]] = []
    cursor = 0
    while True:
        brace = text.find("{", cursor)
        if brace < 0:
            return rules
        selector = text[cursor:brace].strip()
        depth, end = 1, brace + 1
        while end < len(text) and depth:
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
            end += 1
        body = text[brace + 1:end - 1]
        if selector.startswith("@"):
            rules.extend(_rules(body, selector))
        else:
            rules.append((selector, body, context))
        cursor = end


def _selectors(selector: str) -> list[str]:
    return [item.strip() for item in selector.split(",") if item.strip()]


#: 时间单位 → 毫秒（解析 CSS `animation` 简写里的时长）。
_TIME_UNITS_MS = {"ms": 1.0, "s": 1000.0}


def _declarations(body: str) -> dict[str, str]:
    """把一个声明块拆成 `{属性: 值}`（属性名小写）。"""
    out: dict[str, str] = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        name, value = chunk.split(":", 1)
        out[name.strip().lower()] = value.strip()
    return out


def _first_time_ms(value: str) -> float | None:
    """取 `animation` 值里第一个时长（`5s` → 5000，`500ms` → 500）。"""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(ms|s)\b", value or "")
    return float(match.group(1)) * _TIME_UNITS_MS[match.group(2)] if match else None


def _animation_time_ms(declarations: dict[str, str]) -> float | None:
    """一条规则的动画时长（毫秒）：优先读 `animation` 简写，其次 `animation-duration`。"""
    if "animation" in declarations:
        return _first_time_ms(declarations["animation"])
    return _first_time_ms(declarations.get("animation-duration", ""))


def _keyframe_frames(block: str, name: str) -> dict[str, str]:
    """把 `@keyframes <name>` 的 `{帧: 声明块}` 抽出来（如 `{'0%': '...', '50%': '...'}`）。"""
    frames: dict[str, str] = {}
    for selector, body, context in _rules(block):
        if context.strip() != f"@keyframes {name}":
            continue
        for item in _selectors(selector):
            frames[item] = body
    return frames


def _class_level(selector: str) -> int:
    """选择器的「类名级数」（`.a.b.c` → 3）—— CSS 优先级里比的是这个（id / 属性等价更高，这里用不到）。"""
    return len(re.findall(r"\.[-\w]+", selector))


def _new_keyframes(block: str) -> list[str]:
    """v2 新增的关键帧（统一前缀 `office-env-*` / `office-fidget-*`）。

    共享块里早就有 `office-pin-pulse` / `office-pin-glow` 两个工牌关键帧，它们不属于 v2，
    也不受「只动 transform / opacity」这条约束（工牌的光晕本来就靠 box-shadow）。
    """
    return re.findall(r"@keyframes\s+(office-(?:env|fidget)-[\w-]+)", _strip_comments(block))


# --- 1. 零新依赖 ----------------------------------------------------------------------


@needs_frontend
def test_office_env_adds_no_runtime_dependency():
    """PRD 验收 4 / §11.6：纯 CSS 零资源 —— `dependencies` 一个都不许变（含版本）。"""
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"] == FROZEN_DEPENDENCIES, (
        "frontend/package.json 的 dependencies 变了：拟人化 v2 不许引入任何运行时依赖"
    )


@needs_frontend
def test_mobile_shell_cache_version_is_bumped():
    """§11.5：`mobile.css` 属外壳样式表，外壳缓存名必须 +1，否则已装到主屏的应用换不到新样式。"""
    source = SW_JS.read_text(encoding="utf-8")
    # 版本号只增不减：钉住「至少是这次改动之后的版本」，具体数字留给改动方自己 +1
    # （两个人同时改外壳时，谁后合入谁的数字更大，这条守卫不会因为别人再 +1 而误报）。
    versions = [int(match) for match in re.findall(r"arkswarm-m-(?:shell|assets)-v(\d+)", source)]
    assert versions, "sw.js 里找不到带版本号的缓存名"
    assert min(versions) >= 83, f"外壳缓存版本必须 ≥ 83（当前 {min(versions)}）"


# --- 2. 叶子纯函数：没有随机源，种子由 key 派生 ---------------------------------------


@needs_frontend
def test_env_module_is_a_pure_leaf_without_random_sources():
    """§11.1 / §11.2：叶子纯函数，不碰 DOM / 时钟 / 随机源，也不 import React 或组件。"""
    source = _strip_comments(ENV_TS.read_text(encoding="utf-8"))
    for banned in ("Math.random", "Date.now", "performance.now"):
        assert banned not in source, f"lib/collabEnv.ts 里出现了随机源 / 时钟：{banned}"
    assert "return fnv1a32(key)" in source, "workerSeed 必须直接由员工 key 派生（fnv1a32(key)）"
    assert "0x811c9dc5" in source and "0x01000193" in source, "FNV-1a 的两个魔数缺了"
    assert "window." not in source and "document." not in source, "叶子模块不许碰 DOM"
    assert not re.search(r"from '(react|\.\./components/[^']*)'", source), (
        "lib/collabEnv.ts 只允许依赖 ./collabOffice 与 ./collabProgress 的类型，不得 import React / 组件"
    )


@needs_frontend
def test_env_source_freezes_the_documented_arithmetic():
    """把 §11.2 的算式钉死在源码里：常量或算子被人动了一个，这里就红。"""
    source = _strip_comments(ENV_TS.read_text(encoding="utf-8"))
    for token in (
        "export const ENV_TICK_MS = 5000",
        "export const ENV_CYCLE_TICKS = 24",
        "export const ENV_AWAY_TICKS = 6",
        "export const DISCUSS_TICKS = 4",
        "(seed >>> 3)",
        "phase * 7",
        "5200, 6300, 7400, 8500",
        "pending: ['rest']",
        "active: ENV_BEHAVIORS",
        "waiting: []",
        "error: []",
        "done: ENV_BEHAVIORS",
        "FIDGET_ALLOWED: readonly CollabAgentState[] = ['active', 'done']",
        "const ENV_BEHAVIORS: readonly EnvBehavior[] = ['rest', 'walk', 'discuss', 'call']",
    ):
        assert token in source, f"lib/collabEnv.ts 少了冻结口径：{token}"


@needs_frontend
def test_worker_seeds_are_frozen_for_known_keys():
    """种子冻结表：Python 重算的 FNV-1a 必须与冻结表逐格相同（刷新后个性一致的机器保证）。"""
    for key, seed in FROZEN_WORKER_SEEDS.items():
        assert worker_seed(key) == seed, f"{key} 的种子变了：{worker_seed(key)} != {seed}"
    # 换一个 key 也得换个种子（不是常数）。
    assert len(set(FROZEN_WORKER_SEEDS.values())) == len(FROZEN_WORKER_SEEDS)
    # 微动作个性只来自种子：4 档周期 + 负延时（相位错开）。
    for seed in FROZEN_WORKER_SEEDS.values():
        profile = fidget_profile(seed)
        assert profile["kind"] in ("blink", "scratch", "pen", "leg", "glance")
        assert profile["cycleMs"] in (5200, 6300, 7400, 8500)
        assert -profile["cycleMs"] < profile["delayMs"] <= 0


# --- 4. 调度冻结表 --------------------------------------------------------------------


@needs_frontend
def test_env_allowed_matrix_matches_the_architecture_table():
    """§11.3 矩阵：`waiting` / `error` 为 `[]`；`pending` 只放开 `rest`；`active` / `done` 四类全开。

    再反向核对一遍：矩阵里允许的每一类行为，在一整段循环里的**真实调度结果**都要出现过
    （不是「写了不算」，也不是「多给了」）。
    """
    assert ENV_ALLOWED["waiting"] == [] and ENV_ALLOWED["error"] == []
    assert ENV_ALLOWED["pending"] == ["rest"]
    for state in ("active", "done"):
        assert sorted(ENV_ALLOWED[state]) == ["call", "discuss", "rest", "walk"]

    for state, allowed in ENV_ALLOWED.items():
        seats = _seats(state)
        seen = set()
        for tick in range(ENV_CYCLE_TICKS * 2):
            schedule = _env_schedule(seats, tick, False, _BANDS)
            seen.update(entry["env"] for entry in schedule.values() if entry["env"])
        assert seen == set(allowed), f"{state} 的允许集合与调度结果对不上：{sorted(seen)}"


@needs_frontend
def test_waiting_and_error_never_leave_their_pose():
    """§11.3：`waiting`（在等你决定）与 `error`（出问题了）在任何 tick 都不参与环境行为与微动作。"""
    seats = _seats("active")
    for state in ("waiting", "error"):
        for index, seat in enumerate(seats):
            seat["state"] = state if index % 2 else "active"
        for tick in range(ENV_CYCLE_TICKS * 2):
            schedule = _env_schedule(seats, tick, False, _BANDS)
            for seat in seats:
                if seat["state"] in ("waiting", "error"):
                    entry = schedule[seat["key"]]
                    assert entry["env"] is None, f"{seat['state']} 在 tick {tick} 冒出了环境行为"
                    assert entry["fidget"] is None, f"{seat['state']} 在 tick {tick} 冒出了微动作"


@needs_frontend
def test_discuss_always_brings_both_members_together():
    """§11.2：出现 `discuss` 的 tick 上，配对两人**同时**是 `discuss`，且落点收敛到中点。"""
    seats = _seats("active")
    pairs = _discuss_pairs(seats, _BANDS)
    assert pairs, "这一排工位应该能配出讨论搭档"
    chair_by_key = {
        seat["key"]: (seat["desk"][0] + _CHAIR_OFFSET[0], seat["desk"][1] + _CHAIR_OFFSET[1])
        for seat in seats
    }
    discuss_ticks = 0
    for tick in range(ENV_CYCLE_TICKS * 2):
        schedule = _env_schedule(seats, tick, False, _BANDS)
        for pair in pairs:
            entries = [schedule[key] for key in pair["keys"]]
            together = [entry["env"] == "discuss" for entry in entries]
            assert together[0] == together[1], (
                f"tick {tick}：{pair['keys']} 只有一个在讨论 —— 出现「一个人对着空气聊天」"
            )
            if together[0]:
                discuss_ticks += 1
                first = chair_by_key[pair["keys"][0]]
                second = chair_by_key[pair["keys"][1]]
                land_first = (first[0] + entries[0]["dx"], first[1] + entries[0]["dy"])
                land_second = (second[0] + entries[1]["dx"], second[1] + entries[1]["dy"])
                assert abs(land_first[1] - land_second[1]) < 1e-9, "两个人没聚到同一条线上"
                assert abs(land_first[0] - land_second[0]) <= 6, "两个人叠在一起了（残差应 ≤ 6px）"
    assert discuss_ticks > 0, "整段循环里一次讨论都没发生 —— 配对相位算错了？"


@needs_frontend
def test_discuss_is_skipped_when_one_member_cannot_join():
    """§11.2：配对里任一人不允许 `discuss`（`waiting` / `error`）→ 整对跳过。"""
    seats = _seats("active")
    pairs = _discuss_pairs(seats, _BANDS)
    first_pair = pairs[0]
    for seat in seats:
        if seat["key"] == first_pair["keys"][1]:
            seat["state"] = "waiting"
    for tick in range(ENV_CYCLE_TICKS):
        schedule = _env_schedule(seats, tick, False, _BANDS)
        assert schedule[first_pair["keys"][0]]["env"] != "discuss"
        assert schedule[first_pair["keys"][1]]["env"] is None


@needs_frontend
def test_same_tick_differs_between_agents_and_reduced_motion_blocks_moving():
    """PRD 验收 2 / P1-3：同一 tick 里各干各的；`reduced-motion` 下不再有 walk / discuss。"""
    seats = _seats("active")
    combos = set()
    kinds = set()
    for tick in range(ENV_CYCLE_TICKS):
        schedule = _env_schedule(seats, tick, False, _BANDS)
        combos.add(tuple(sorted((key, entry["env"] or "", entry["fidget"] or "")
                                for key, entry in schedule.items())))
        kinds.update(entry["fidget"] for entry in schedule.values() if entry["fidget"])
    assert len(combos) > 1, "每一格的全场动作都一样 —— 差异化没生效"
    assert len(kinds) >= 2, f"同屏微动作种类太单一：{kinds}"

    for tick in range(ENV_CYCLE_TICKS):
        schedule = _env_schedule(seats, tick, True, _BANDS)
        for key, entry in schedule.items():
            assert entry["env"] in (None, "rest", "call"), (
                f"reduced-motion 下 {key} 出现了依赖位移的环境行为：{entry['env']}"
            )
            assert entry["dx"] == 0 and entry["dy"] == 0, "reduced-motion 下不该有位移"


@needs_frontend
def test_walk_step_is_about_one_iso_grid_step():
    """§11.4：`OFFICE_WALK_STEP` 由 `OFFICE_CHAIR_OFFSET`（0.74 格）反推，取 1.2～2 格。"""
    source = ENV_TS.read_text(encoding="utf-8")
    assert "OFFICE_WALK_STEP" in source
    grid = (abs(_CHAIR_OFFSET[0]) / 0.74, abs(_CHAIR_OFFSET[1]) / 0.74)
    step = (abs(_CHAIR_OFFSET[0] / 0.74 * 1.6), abs(_CHAIR_OFFSET[1] / 0.74 * 1.6))
    ratio_x, ratio_y = step[0] / grid[0], step[1] / grid[1]
    assert 1.2 <= ratio_x <= 2 and 1.2 <= ratio_y <= 2
    assert abs(ratio_x - ratio_y) < 1e-9, "步长要与椅子偏移同方向（等距底图的一格）"
    assert "OFFICE_CHAIR_OFFSET.x / 0.74" in source and "OFFICE_CHAIR_OFFSET.y / 0.74" in source, (
        "OFFICE_WALK_STEP 必须由 OFFICE_CHAIR_OFFSET 反推（不许另写一组坐标）"
    )


# --- 5. 样式：全部落在两端共享块内，且不给 error / waiting 开口子 ----------------------


@needs_frontend
def test_env_and_fidget_styles_live_in_the_shared_block_on_both_ends():
    """§11.5：4 类环境行为 + 5 类微动作 + 新增关键帧都在两端共享块里（守卫会逐字比对）。"""
    for name, block in _office_blocks():
        for token in (".env-rest", ".env-walk", ".env-discuss", ".env-call",
                      ".fidget-blink", ".fidget-scratch", ".fidget-pen", ".fidget-leg", ".fidget-glance"):
            assert token in block, f"{name} 的共享块里缺少 {token} 的样式"
        keyframes = _new_keyframes(block)
        assert "office-env-walk" in keyframes and "office-env-discuss" in keyframes, keyframes
        for kind in ("blink", "scratch", "pen", "leg", "glance"):
            assert f"office-fidget-{kind}" in keyframes, f"{name} 缺少 office-fidget-{kind} 关键帧"


@needs_frontend
def test_no_env_rule_opens_the_door_for_error_or_waiting():
    """§11.3：`state-error` / `state-waiting` 不许出现在任何 `.env-*` 组合选择器里（CSS 硬开口子）。"""
    for name, block in _office_blocks():
        for selector, _body, _context in _rules(block):
            if ".env-" not in selector:
                continue
            assert "state-error" not in selector, f"{name} 用 CSS 给「出错」开了环境行为：{selector}"
            assert "state-waiting" not in selector, f"{name} 用 CSS 给「等你决定」开了环境行为：{selector}"


@needs_frontend
def test_office_scale_is_defined_exactly_once():
    """§11.4：`--office-scale: 0.72` 只从 `.office-worker > .collab-station-scene` 的 `scale()` 里定义一处。"""
    for name, block in _office_blocks():
        assert block.count("--office-scale: 0.72") == 1, f"{name} 里的 --office-scale 定义不止一处"
        assert "scale(var(--office-scale))" in block, f"{name} 的 scale() 没用上 --office-scale"
        assert "calc(var(--env-x) / var(--office-scale))" in block, (
            f"{name} 的身体位移没有按 --office-scale 换算（尺子得是一把）"
        )
    for name, block in _office_blocks():
        assert "!important" not in _strip_comments(block), (
            f"{name} 的共享块里用了 !important（reduced-motion 段之外不许用）"
        )


# --- 6 / 7. reduced-motion 覆盖 + 关键帧属性白名单 ------------------------------------


@needs_frontend
def test_reduced_motion_silences_every_new_keyframe():
    """§11.5：共享块内的 reduced-motion 段必须覆盖**所有**新增关键帧的载体选择器。"""
    for name, block in _office_blocks():
        names = _new_keyframes(block)
        assert names, f"{name} 的共享块里没有新增关键帧"
        rules = _rules(block)
        carriers: dict[str, list[str]] = {}
        for selector, body, context in rules:
            if context:  # 只看顶层规则；reduced-motion 自己的规则不算载体
                continue
            for keyframe in names:
                if "animation" in body and keyframe in body:
                    carriers.setdefault(keyframe, []).extend(_selectors(selector))
        muted: set[str] = set()
        media_rules = [rule for rule in rules if "prefers-reduced-motion" in rule[2]]
        assert media_rules, f"{name} 的共享块里缺少 prefers-reduced-motion 覆盖段"
        for selector, body, _context in media_rules:
            assert "animation: none" in body, f"{name} 的 reduced-motion 段里没有 animation: none"
            muted.update(_selectors(selector))
        assert set(carriers) == set(names), (
            f"{name}：有新增关键帧没被任何规则引用：{sorted(set(names) - set(carriers))}"
        )
        for keyframe, selectors in carriers.items():
            for selector in selectors:
                assert selector in muted, (
                    f"{name}：{keyframe} 的载体 `{selector}` 没在 reduced-motion 段里被静默"
                )


@needs_frontend
def test_new_keyframes_only_animate_transform_and_opacity():
    """PRD 约束 9 / §11.6：新增关键帧只动 `transform` / `opacity`（避免 layout thrash）。"""
    allowed = {"transform", "opacity", "from", "to"}
    for name, block in _office_blocks():
        seen = set()
        for _selector, body, context in _rules(block):
            if not context.startswith("@keyframes"):
                continue
            keyframe = context.split()[1]
            if not keyframe.startswith(("office-env-", "office-fidget-")):
                continue
            seen.add(keyframe)
            properties = set(re.findall(r"([a-zA-Z-]+)\s*:", body))
            assert properties <= allowed, f"{name}：{keyframe} 动了白名单外的属性：{properties - allowed}"
        assert seen == set(_new_keyframes(block)), f"{name}：关键帧没有全部扫到"


# --- 8. 返工：walk 对齐 tick（无瞬移）+ env 手臂压过 L1 打字 -------------------------


@needs_frontend
def test_walk_animation_is_tick_aligned_and_home_bound():
    """打回 1：`walk` 动画周期 == ENV_TICK_MS，且关键帧首末帧钉在工位（零位移）。

    调度器只在**每个 tick 边界**加 / 摘 `.env-walk` 类。只要动画周期正好是一格（5s），
    边界处动画就落在 0% / 100% 帧上；再把首末帧钉成 `translate3d(0,0,0)`、不留满位移平台，
    摘类那一刻 `transform` 恰好归零 —— 不会残留半程位移把人瞬移回过道 / 工位之间。
    `discuss` 同理：讨论窗口 `DISCUSS_TICKS×ENV_TICK_MS`（20s）必须是 discuss 周期的整数倍。
    """
    for name, block in _office_blocks():
        # (a) walk 的身体层动画周期必须等于 ENV_TICK_MS。
        walk_ms: list[float] = []
        for selector, body, context in _rules(block):
            if context or ".env-walk" not in selector or ".collab-worker-body" not in selector:
                continue
            ms = _animation_time_ms(_declarations(body))
            assert ms is not None, f"{name}: .env-walk 身体层没有动画时长：{selector}"
            walk_ms.append(ms)
        assert walk_ms, f"{name}: 找不到 `.env-walk .collab-worker-body` 动画规则"
        for ms in walk_ms:
            assert ms == ENV_TICK_MS, (
                f"{name}: walk 动画周期 {ms}ms != ENV_TICK_MS({ENV_TICK_MS}ms) —— "
                "tick 边界对不齐，摘类必留下位移（瞬移）"
            )

        # (b) 首末帧钉在工位：0% / 100% 不带 `var(--env…)` 位移，且就是零位移。
        frames = _keyframe_frames(block, "office-env-walk")
        assert "0%" in frames and "100%" in frames, f"{name}: office-env-walk 缺 0% / 100% 帧"
        for frame in ("0%", "100%"):
            value = frames[frame]
            assert "var(--env" not in value, (
                f"{name}: office-env-walk 的 {frame} 帧带了 --env 位移，摘类会残留：{value!r}"
            )
            assert re.search(r"translate3d\(\s*0(?:px)?\s*,\s*0(?:px)?\s*,\s*0(?:px)?\s*\)", value), (
                f"{name}: office-env-walk 的 {frame} 帧不是零位移：{value!r}"
            )
        # (c) 中间必须真的走到过道（否则 walk 退化成原地不动），且不再有「满位移平台」帧。
        middle = {frame: body for frame, body in frames.items() if frame not in ("0%", "100%")}
        assert middle, f"{name}: office-env-walk 只有首末帧，没走到过道"
        assert any("var(--env" in body for body in middle.values()), (
            f"{name}: office-env-walk 中途没有 --env 位移 —— walk 没真的离开工位"
        )

        # (d) discuss 同理：讨论窗口必须是 discuss 周期的整数倍（当前 20s / 10s）。
        discuss_ms: float | None = None
        for selector, body, context in _rules(block):
            if context or ".env-discuss" not in selector or ".collab-worker-body" not in selector:
                continue
            discuss_ms = _animation_time_ms(_declarations(body))
        assert discuss_ms, f"{name}: 找不到 `.env-discuss .collab-worker-body` 动画规则"
        window_ms = DISCUSS_TICKS * ENV_TICK_MS
        assert window_ms % discuss_ms == 0, (
            f"{name}: 讨论窗口 {window_ms}ms 不是 discuss 周期 {discuss_ms}ms 的整数倍 —— "
            "摘类会停在半程位移"
        )


@needs_frontend
def test_env_arm_rules_outrank_typing_on_the_same_arm():
    """打回 2：给手臂上打字动画的规则（`collab-typing-*`）不得比 `env-rest` / `env-call` 的手臂规则更高。

    用 `(类名级数, 源序)` 二元组比优先级：级数更大、或同级但源序更靠后 → 更高。要求打字规则
    **严格低于**对应的环境行为手臂规则。这样 `state-active` 敲键盘 / `live` 常驻打字的工位叠加
    `env-rest` / `env-call` 时，手臂归环境行为（两手 / 右臂不再出现打字动画），§11.6 部件分工表成立。
    """
    for name, block in _office_blocks():
        typing: dict[str, list[tuple[int, int]]] = {"left": [], "right": []}
        env_arms: dict[str, list[tuple[int, int]]] = {"left": [], "right": []}
        for order, (selector, body, context) in enumerate(_rules(block)):
            if context:
                continue
            for item in _selectors(selector):
                if re.search(r"\.collab-worker-arm\.left\b", item):
                    arm = "left"
                elif re.search(r"\.collab-worker-arm\.right\b", item):
                    arm = "right"
                else:
                    continue
                if "collab-typing-" in body:
                    typing[arm].append((_class_level(item), order))
                if (".env-rest" in item or ".env-call" in item) and ("transform" in body or "animation" in body):
                    env_arms[arm].append((_class_level(item), order))
        for arm in ("left", "right"):
            assert typing[arm], f"{name}: 找不到 {arm} 手臂的打字（collab-typing-*）规则"
            assert env_arms[arm], f"{name}: 找不到 {arm} 手臂的 env-rest / env-call 规则"
            worst_typing = max(typing[arm])
            best_env = min(env_arms[arm])
            assert worst_typing < best_env, (
                f"{name}：{arm} 手臂上打字规则的 (级数, 源序)={worst_typing} 不低于环境行为的 {best_env} —— "
                "env 手臂会被 L1 打字盖住（打回 2 复发）"
            )


# --- 组件接线（不靠肉眼核对「功能真的接上了」） ---------------------------------------


@needs_frontend
def test_office_map_wires_tick_mediaquery_and_per_seat_flags():
    """ENV-3：容器层持有 tick 与媒体查询，把 `--seed / --env-x / --env-y / env-* / fidget-*` 贴到工位上。"""
    source = MAP_TSX.read_text(encoding="utf-8")
    for token in (
        "setInterval", "clearInterval", "ENV_TICK_MS", "envScheduleFor(", "sameSchedule(",
        "matchMedia", "prefers-reduced-motion", "data-seat-key", "--seed", "--env-x", "--env-y",
        "officeChairPoint(",
    ):
        assert token in source, f"CollabOfficeMap.tsx 里缺少 {token}"
    assert source.count("officeChairPoint(") >= 2, "小人与工牌都要用 officeChairPoint() 定位"
    assert "state-${seat.node.state}" in source, "真实状态类名不许被改口径"
    assert "env-${env.env}" in source and "fidget-${env.fidget}" in source
    # 信息卡不参与环境行为（§11.5）。
    assert "WorkerAtDesk state={focused.node.state}" in source


@needs_frontend
def test_worker_keeps_the_chair_in_place_and_puts_the_body_on_top_of_it():
    """ENV-2：椅子留在 `.collab-worker` 直下不动，气泡 / 头 / 躯干 / 手臂 / 腿 / 杯子都进身体层。"""
    source = WORKER_TSX.read_text(encoding="utf-8")
    assert 'className="collab-worker-chair"' in source
    assert 'className="collab-worker-body"' in source
    assert source.index("collab-worker-chair") < source.index("collab-worker-body"), (
        "椅子要画在身体层之外（转椅不跟着走位）"
    )
    for token in ("collab-worker-leg left", "collab-worker-leg right", "collab-worker-cup",
                  "collab-worker-bubble", "collab-worker-head", "collab-worker-torso",
                  "collab-worker-arm left", "collab-worker-arm right"):
        assert token in source, f"CollabWorker.tsx 里缺少 {token}"
    assert "fidgetProfile(seed)" in source and "--fidget-delay" in source and "--fidget-cycle" in source
    assert "env?: EnvBehavior | null" in source and "seed?: number" in source, (
        "env / seed 必须是可选 props（不传时姿态与改造前等价）"
    )
    desk = DESK_TSX.read_text(encoding="utf-8")
    assert "<CollabWorker state={state} env={env} seed={seed} />" in desk
