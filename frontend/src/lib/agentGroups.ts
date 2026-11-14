import type { AgentProfile } from './types'

/** 未分组那一堆的 key：用不会与真实分组名撞上的哨兵值，别用空串（空串本身就是「未分组」的存储值）。 */
export const UNGROUPED_KEY = '\u0000ungrouped'

export interface AgentGroup {
  key: string
  label: string
  items: AgentProfile[]
}

/**
 * 按分组把智能体分堆（两端共用这一份纯逻辑）。
 *
 * 未分组（`group_name` 为空）永远排在最后：它是兜底的那一堆，不该插在真实分组中间；
 * 其余按分组名排序，中文按拼音。同一个分组里的顺序保持接口返回的顺序（按创建时间）。
 */
export function groupAgents(agents: AgentProfile[]): AgentGroup[] {
  const buckets = new Map<string, AgentProfile[]>()
  for (const agent of agents) {
    const key = (agent.group_name ?? '').trim()
    const bucket = buckets.get(key) ?? []
    bucket.push(agent)
    buckets.set(key, bucket)
  }
  return [...buckets.entries()]
    .map(([key, items]) => ({ key: key || UNGROUPED_KEY, label: key || '未分组', items }))
    .sort((left, right) => {
      if (left.key === UNGROUPED_KEY) return 1
      if (right.key === UNGROUPED_KEY) return -1
      return left.label.localeCompare(right.label, 'zh-Hans-CN')
    })
}
