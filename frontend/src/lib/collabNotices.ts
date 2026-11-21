/**
 * 提醒的「已关闭」记忆：用户点过「×」的提醒，刷新后不要再冒出来。
 *
 * 只记**可重建**的提醒（失败步骤，key 形如 `step:<id>`）：临时提醒（审计、诊断）刷新后
 * 本来就不存在，没必要记。存储读写失败（隐私模式、配额用尽）一律降级为「没有记忆」，
 * 不影响界面本身。
 */
const STORAGE_KEY = 'coda.collab.notices.dismissed'
/** 最多记住多少条运行：超出的按最旧淘汰，避免本地存储无限膨胀。 */
const MAX_RUNS = 20
/** 单个运行最多记多少条。 */
const MAX_KEYS = 50

type Store = Record<string, string[]>

function readStore(): Store {
  if (typeof localStorage === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const store: Store = {}
    for (const [runId, keys] of Object.entries(parsed as Record<string, unknown>)) {
      if (Array.isArray(keys)) store[runId] = keys.filter((key): key is string => typeof key === 'string')
    }
    return store
  } catch {
    // 本地存储不可用（隐私模式 / 数据损坏）：当作没有记忆，界面照常工作
    return {}
  }
}

function writeStore(store: Store): void {
  if (typeof localStorage === 'undefined') return
  try {
    const runIds = Object.keys(store)
    for (const stale of runIds.slice(0, Math.max(0, runIds.length - MAX_RUNS))) delete store[stale]
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store))
  } catch {
    // 写不进去就算了：最坏情况是刷新后提醒又出现，不影响推进
  }
}

/** 读某个运行下已被用户关掉的提醒（key 集合）。 */
export function loadDismissedNotices(runId: string): Set<string> {
  if (!runId) return new Set()
  return new Set(readStore()[runId] ?? [])
}

/** 记住「这条提醒用户关掉了」。 */
export function rememberDismissedNotice(runId: string, key: string): void {
  if (!runId || !key) return
  const store = readStore()
  const keys = new Set(store[runId] ?? [])
  keys.add(key)
  store[runId] = [...keys].slice(-MAX_KEYS)
  writeStore(store)
}

/** 运行被删除时清掉它的记忆，别留在本地。 */
export function forgetDismissedNotices(runId: string): void {
  const store = readStore()
  if (!(runId in store)) return
  delete store[runId]
  writeStore(store)
}
