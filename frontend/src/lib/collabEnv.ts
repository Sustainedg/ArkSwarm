/**
 * 协作办公室「拟人化 v2」的**叶子纯函数模块**（工程口径见 `docs/architecture.md` §11）。
 *
 * 这里只做整数运算：
 * - 不 import React / 任何组件 / 网络；
 * - 不读时钟、不取随机源（**禁止** `Math.random` / `Date.now` / `performance.now`）。
 *
 * 所以「同一个员工刷新后 / 换浏览器后个性一致」有一条**客观可复核**的验收路径：
 * `backend/tests/test_office_env.py` 用 Python 逐字镜像本文件的算术，两边算不出同一个数就会红。
 *
 * 依赖方向（单向）：只读 `./collabOffice` 的类型与常量（工位坐标 / 椅子落点），不被组件反向依赖。
 */
import {
  OFFICE_CHAIR_OFFSET, OFFICE_DESK_ROWS, officeChairPoint,
  type OfficeDesk, type OfficeSeat,
} from './collabOffice'
import type { CollabAgentState } from './collabProgress'

/** L2 环境行为（叠加在真实状态姿势之上，不替换状态语义）。 */
export type EnvBehavior = 'rest' | 'walk' | 'discuss' | 'call'
/** L3 微动作（五官 / 手 / 腿的小幅动作）。 */
export type FidgetKind = 'blink' | 'scratch' | 'pen' | 'leg' | 'glance'

/** 调度粒度：一格 5 秒。 */
export const ENV_TICK_MS = 5000
/** 一个循环 24 格 = 2 分钟。 */
export const ENV_CYCLE_TICKS = 24
/** 每人每循环最多 6 格（30 秒）在做环境行为。 */
export const ENV_AWAY_TICKS = 6
/** 一对人凑到一起讨论的窗口：4 格 = 20 秒。 */
export const DISCUSS_TICKS = 4

/** 把字符串按 **UTF-8 字节**展开（与 Python 的 `text.encode('utf-8')` 逐字节一致）。 */
function utf8Bytes(text: string): number[] {
  const bytes: number[] = []
  for (let index = 0; index < text.length; index += 1) {
    let code = text.charCodeAt(index)
    // 代理对（astral 字符）：合成一个码点再按 4 字节展开。
    if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length) {
      const next = text.charCodeAt(index + 1)
      if (next >= 0xdc00 && next <= 0xdfff) {
        code = 0x10000 + ((code - 0xd800) << 10) + (next - 0xdc00)
        index += 1
      }
    }
    if (code < 0x80) bytes.push(code)
    else if (code < 0x800) bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f))
    else if (code < 0x10000) {
      bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f))
    } else {
      bytes.push(
        0xf0 | (code >> 18), 0x80 | ((code >> 12) & 0x3f),
        0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f),
      )
    }
  }
  return bytes
}

/**
 * 32 位 FNV-1a（`docs/architecture.md` §11.2 的伪码，逐字实现）：
 *
 * ```
 * h = 0x811c9dc5
 * for byte b of utf8(text): h = ((h ^ b) * 0x01000193) & 0xffffffff
 * ```
 */
export function fnv1a32(text: string): number {
  let hash = 0x811c9dc5
  for (const byte of utf8Bytes(text)) {
    hash = Math.imul(hash ^ byte, 0x01000193) >>> 0
  }
  return hash
}

/** 员工稳定种子：同一个 `key` 在桌面端 / 移动端 / 刷新后 / 换浏览器都是同一个数。 */
export function workerSeed(key: string): number {
  return fnv1a32(key)
}

const ENV_BEHAVIORS: readonly EnvBehavior[] = ['rest', 'walk', 'discuss', 'call']

/**
 * 状态覆盖矩阵（`docs/architecture.md` §11.3）：环境行为只在安全状态上叠加。
 *
 * `waiting`（在等你决定）与 `error`（出问题了）一律不参与 —— 这两类状态最需要用户立刻注意，
 * 不能被演成悠闲。
 */
export const ENV_ALLOWED: Record<CollabAgentState, readonly EnvBehavior[]> = {
  pending: ['rest'],
  active: ENV_BEHAVIORS,
  waiting: [],
  error: [],
  done: ENV_BEHAVIORS,
}

/**
 * 允许微动作的真实状态（§11.3 矩阵的最后一行）。
 *
 * `pending` 闭眼、`waiting` 举手、`error` 瞪眼，这三套姿势与「眨眼 / 挠头 / 转笔 / 抖腿 / 张望」
 * 会抢同一个部件，所以不开微动作。
 */
export const FIDGET_ALLOWED: readonly CollabAgentState[] = ['active', 'done']

/**
 * 个人调度（§11.2 的伪码，逐字实现）：
 *
 * ```
 * if (allowed.length === 0) return null
 * phase = (tick + (seed >>> 3)) % ENV_CYCLE_TICKS
 * if (phase >= ENV_AWAY_TICKS) return null
 * return allowed[(seed + phase * 7) % allowed.length]
 * ```
 */
export function envBehaviorFor(
  seed: number,
  tick: number,
  allowed: readonly EnvBehavior[],
): EnvBehavior | null {
  if (allowed.length === 0) return null
  const phase = (tick + (seed >>> 3)) % ENV_CYCLE_TICKS
  if (phase >= ENV_AWAY_TICKS) return null
  return allowed[(seed + phase * 7) % allowed.length]
}

const FIDGET_KINDS: readonly FidgetKind[] = ['blink', 'scratch', 'pen', 'leg', 'glance']
/** 微动作周期 4 档：5.2 / 6.3 / 7.4 / 8.5 秒（§11.2）。 */
const FIDGET_CYCLES: readonly number[] = [5200, 6300, 7400, 8500]

/**
 * 微动作个性（§11.2）：`kind` 在 5 种小动作里按 `seed` 选，`cycleMs` 取 4 档，
 * `delayMs` 取**负值**（把相位往后挪），使同屏员工的呼吸 / 眨眼 / 张望互不同频。
 */
export function fidgetProfile(seed: number): { kind: FidgetKind; cycleMs: number; delayMs: number } {
  const kind = FIDGET_KINDS[seed % FIDGET_KINDS.length]
  const cycleMs = FIDGET_CYCLES[(seed >>> 4) % FIDGET_CYCLES.length]
  const delayMs = -(seed % cycleMs)
  return { kind, cycleMs, delayMs }
}

/** 一对讨论搭档。 */
export interface DiscussPair {
  keys: string[]
  seed: number
}

/**
 * 某张桌子属于「第几排」：行与行的 y 完全不同，取最近的排首。
 *
 * 用坐标判定而不是下标，是因为绕圈的人（`seatFlowRows` 的 `lap`）会往后挪一点，
 * 但仍在同一排的 y 上。
 */
function rowIndexFor(desk: OfficeDesk): number {
  let best = 0
  let bestDistance = Number.POSITIVE_INFINITY
  OFFICE_DESK_ROWS.forEach((row, index) => {
    if (row.length === 0) return
    const distance = Math.abs(row[0].y - desk.y)
    if (distance < bestDistance) {
      bestDistance = distance
      best = index
    }
  })
  return best
}

/**
 * 凑一起讨论必须真的「凑一起」（§11.2）：分组由**座位布局**决定 ——
 * 在每一排内部把相邻两人两两配对（一排只剩一个人时不参与讨论）；
 * 每对有 `pairSeed = workerSeed('discuss:' + 两人 key)`。
 */
export function discussPairs<T extends { key: string }>(seats: OfficeSeat<T>[]): DiscussPair[] {
  const rows = new Map<number, OfficeSeat<T>[]>()
  for (const seat of seats) {
    const index = rowIndexFor(seat.desk)
    const list = rows.get(index)
    if (list) list.push(seat)
    else rows.set(index, [seat])
  }
  const pairs: DiscussPair[] = []
  const indices = [...rows.keys()].sort((a, b) => a - b)
  for (const index of indices) {
    const list = [...(rows.get(index) ?? [])].sort((a, b) => (
      a.desk.x - b.desk.x || (a.node.key < b.node.key ? -1 : a.node.key > b.node.key ? 1 : 0)
    ))
    for (let cursor = 0; cursor + 1 < list.length; cursor += 2) {
      const keys = [list[cursor].node.key, list[cursor + 1].node.key]
      pairs.push({ keys, seed: workerSeed(`discuss:${keys.join('+')}`) })
    }
  }
  return pairs
}

/**
 * 聚点 = 同排配对两人椅点的**中点**（`officeChairPoint(desk)` 现算，不落成常量）；
 * 每人的位移 = 中点 − 自己椅点，两人位移相反 → 面对面聚拢。
 *
 * 各留 ±3px 的残差（两人相距 6px）：既「凑到一起」，又不完全重叠成一团。
 */
export function envOffsets(chairs: OfficeDesk[]): OfficeDesk[] {
  if (chairs.length < 2) return chairs.map(() => ({ x: 0, y: 0 }))
  const midX = (chairs[0].x + chairs[1].x) / 2
  const midY = (chairs[0].y + chairs[1].y) / 2
  return chairs.map((chair, index) => ({
    x: midX - chair.x + (index === 0 ? 3 : -3),
    y: midY - chair.y,
  }))
}

/**
 * 一格等距步长的方向：由 `OFFICE_CHAIR_OFFSET`（桌心 → 转椅 = 0.74 格）**反推**，
 * 取 1.6 格 —— `walk` 时人朝本排工位前的过道走这么远（画布像素）。
 *
 * 不新增任何工位坐标：所有人从自己的椅子朝**同一方向**平移，位置天然错开。
 */
export const OFFICE_WALK_STEP: OfficeDesk = {
  x: (OFFICE_CHAIR_OFFSET.x / 0.74) * 1.6,
  y: (OFFICE_CHAIR_OFFSET.y / 0.74) * 1.6,
}

/** 一个工位在某一格的行为（L2 + L3）与位移。 */
export interface EnvState {
  /** L2 环境行为；`null` = 保持真实状态姿势。 */
  env: EnvBehavior | null
  /** L3 微动作；`null` = 该状态不参与微动作（§11.3 矩阵）。 */
  fidget: FidgetKind | null
  /** 画布像素位移（`walk` / `discuss` 用；其余为 0）。 */
  dx: number
  dy: number
}

/**
 * 一格（tick）里「工位 key → 行为」的完整派生：**组件层只负责把它贴到 DOM 上**，
 * 所有算术都在这个纯函数里 —— 这样 Python 守卫可以逐字镜像（§11.7）。
 *
 * 三层口径：
 * 1. 个人调度 `envBehaviorFor(workerSeed(key), tick, allowed)`；
 * 2. `discuss` 覆盖：只有同一排配对的两人才会**同时**进入讨论，任一人不允许（`waiting` / `error`）就整对跳过；
 * 3. `reduced-motion` 下把允许集合收窄到 `rest` / `call`，屏蔽依赖位移的 `walk` / `discuss`
 *    （动画一停，位移就成了每秒瞬移，那不算「静默」）。
 */
export function envScheduleFor<T extends { key: string; state: CollabAgentState }>(
  seats: readonly OfficeSeat<T>[],
  tick: number,
  reducedMotion: boolean,
): Record<string, EnvState> {
  const allowedOf = (state: CollabAgentState): readonly EnvBehavior[] => {
    const allowed = ENV_ALLOWED[state]
    return reducedMotion ? allowed.filter((item) => item === 'rest' || item === 'call') : allowed
  }
  const schedule: Record<string, EnvState> = {}
  for (const seat of seats) {
    const seed = workerSeed(seat.node.key)
    const allowed = allowedOf(seat.node.state)
    // `discuss` 不由个人决定（§11.2「凑一起」）：个人调度里把它去掉，
    // 只有下面那一步的**配对窗口**才会让两个人同时进入讨论。
    const personal = allowed.filter((item) => item !== 'discuss')
    schedule[seat.node.key] = {
      env: envBehaviorFor(seed, tick, personal),
      fidget: FIDGET_ALLOWED.includes(seat.node.state) ? fidgetProfile(seed).kind : null,
      dx: 0,
      dy: 0,
    }
  }

  for (const pair of discussPairs([...seats])) {
    const pairPhase = (tick + (pair.seed >>> 3)) % ENV_CYCLE_TICKS
    if (pairPhase >= DISCUSS_TICKS) continue
    const members = pair.keys.map((key) => seats.find((seat) => seat.node.key === key))
    if (members.some((seat) => !seat)) continue
    if (!members.every((seat) => allowedOf((seat as OfficeSeat<T>).node.state).includes('discuss'))) continue
    const offsets = envOffsets(members.map((seat) => officeChairPoint((seat as OfficeSeat<T>).desk)))
    pair.keys.forEach((key, index) => {
      const current = schedule[key]
      schedule[key] = { ...current, env: 'discuss', dx: offsets[index].x, dy: offsets[index].y }
    })
  }

  for (const seat of seats) {
    const current = schedule[seat.node.key]
    if (current.env === 'walk') {
      current.dx = OFFICE_WALK_STEP.x
      current.dy = OFFICE_WALK_STEP.y
    }
  }
  return schedule
}
