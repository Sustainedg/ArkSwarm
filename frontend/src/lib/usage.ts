/**
 * 把各家智能体上报的 usage 归一化，并汇总出整段对话的 token 消耗。
 *
 * 不同适配器的字段名并不统一：Claude Code 用 input_tokens /
 * cache_read_input_tokens，OpenAI 兼容端点用 prompt_tokens / cached_tokens，
 * Codex 用 input_tokens / cached_input_tokens。这里统一成一组展示字段。
 */

export interface UsageSummary {
  /** 输入（提示词）token 数 */
  input: number
  /** 输出（补全）token 数 */
  output: number
  /** 总 token 数 */
  total: number
  /** 命中缓存的输入 token 数 */
  cacheRead: number
  /** 写入缓存的输入 token 数 */
  cacheCreation: number
  /** 缓存命中率（0~1），无数据时为 null */
  cacheRatio: number | null
  /** 参与统计的消息条数 */
  messages: number
}

const EMPTY: UsageSummary = {
  input: 0, output: 0, total: 0, cacheRead: 0, cacheCreation: 0, cacheRatio: null, messages: 0,
}

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function pick(usage: Record<string, unknown>, keys: string[]): number {
  for (const key of keys) {
    if (key in usage) return num(usage[key])
  }
  return 0
}

/** 是否采用「缓存 token 与 input_tokens 分离」的记法（Claude Code 风格）。 */
function isExclusiveCache(usage: Record<string, unknown>): boolean {
  return 'cache_read_input_tokens' in usage || 'cache_creation_input_tokens' in usage
}

export function summarizeUsage(usages: (Record<string, unknown> | null | undefined)[]): UsageSummary {
  const summary: UsageSummary = { ...EMPTY }
  let cacheBase = 0

  for (const usage of usages) {
    if (!usage || Object.keys(usage).length === 0) continue
    const input = pick(usage, ['input_tokens', 'prompt_tokens'])
    const output = pick(usage, ['output_tokens', 'completion_tokens'])
    const cacheRead = pick(usage, [
      'cache_read_input_tokens', 'cached_input_tokens', 'cached_tokens',
      'prompt_cache_hit_tokens', 'cache_read_tokens',
    ])
    const cacheCreation = pick(usage, ['cache_creation_input_tokens', 'cache_creation_tokens'])
    const total = pick(usage, ['total_tokens']) || input + output + cacheRead + cacheCreation

    summary.messages += 1
    summary.input += input
    summary.output += output
    summary.total += total
    summary.cacheRead += cacheRead
    summary.cacheCreation += cacheCreation
    // 分离式记法下，input_tokens 不含缓存，需把缓存计入分母；inclusive 记法则不用。
    cacheBase += isExclusiveCache(usage) ? input + cacheRead + cacheCreation : input
  }

  summary.cacheRatio = cacheBase > 0 ? summary.cacheRead / cacheBase : null
  return summary
}

/* ---------- 用量页的时间范围（两端共用这一份口径）---------- */

/** 用量页可选的预设范围；`custom` 用用户填的起止日期。 */
export type UsageRangeKind = 'today' | 'week' | 'month' | 'custom' | 'all'

export interface UsageRangeQuery {
  /** 起始时刻（ISO 8601，含）*/
  start?: string
  /** 结束时刻（ISO 8601，不含）*/
  end?: string
  /** 本地时区相对 UTC 的分钟偏移（东为正）；只用于「按天」分桶 */
  tz_offset: number
}

/** 一个本地自然日 00:00（按本机时区，而不是 UTC 零点）。 */
function localMidnight(base: Date, dayOffset: number): Date {
  return new Date(base.getFullYear(), base.getMonth(), base.getDate() + dayOffset)
}

/** 把 `YYYY-MM-DD` 解析成当地那天的 00:00；格式不对返回 null。 */
function parseDay(value: string | undefined): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? '')
  if (!match) return null
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
}

/**
 * 把预设范围换算成接口参数。区间一律按**本机时区**的整天取（`end` 是次日的 00:00，开区间），
 * 这样「今日 / 一周」的边界与用户看到的日期一致；`all` 不带上限。
 */
export function usageRangeQuery(
  kind: UsageRangeKind,
  customStart?: string,
  customEnd?: string,
): UsageRangeQuery {
  const tzOffset = -new Date().getTimezoneOffset()
  if (kind === 'all') return { tz_offset: tzOffset }

  const now = new Date()
  const today = localMidnight(now, 0)
  let start = today
  let end = localMidnight(now, 1)

  if (kind === 'week') {
    start = localMidnight(now, -6)
  } else if (kind === 'month') {
    start = localMidnight(now, -29)
  } else if (kind === 'custom') {
    start = parseDay(customStart) ?? today
    // 结束日含当天：取那天的次日 00:00 作为开区间上界。
    end = localMidnight(parseDay(customEnd) ?? now, 1)
  }

  return { start: start.toISOString(), end: end.toISOString(), tz_offset: tzOffset }
}

/** 把日期格式化成 `<input type="date">` 需要的 `YYYY-MM-DD`（按本机时区）。 */
export function dayInputValue(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

