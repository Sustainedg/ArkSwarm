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
