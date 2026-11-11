/**
 * 把一个岗位在协作里的「对话上下文」拼出来。
 *
 * 协作是串行的交接链：上游角色派工时的发言 + 这张工单的要点，就是下游角色**收到的话**。
 * 岗位抽屉据此按时间顺序展示「谁对我说了什么 → 我做了什么」，而不是只列我自己的步骤；
 * 入口领导者还额外接收用户的开场消息。本模块不碰 DOM、不发请求，桌面端与移动端共用。
 */
import type { CollabStep, CollabTicket } from './types'

export type RoleThreadKind = 'user' | 'handoff' | 'work'

export interface RoleThreadEntry {
  id: string
  kind: RoleThreadKind
  sequence: number
  /** user / handoff：来源节点 key 与显示名（用户没有 key） */
  fromKey?: string
  fromLabel?: string
  fromAvatar?: string
  /** user / handoff：对方说的话（派工正文，去掉 work_order 块） */
  text?: string
  /** handoff：交接的工单（用于展示任务要点） */
  ticket?: CollabTicket
  /** work：本岗位自己的那一步 */
  step?: CollabStep
}

export interface RoleThreadInput {
  nodeKey: string
  isEntry: boolean
  steps: CollabStep[]
  tickets: CollabTicket[]
  labelOf: (key: string) => string
  avatarOf: (key: string) => string
}

/** 去掉交接正文里可能残留的围栏代码块，只留角色说的人话。 */
function readableMessage(text: string | null | undefined): string {
  const cleaned = (text ?? '').replace(/```[a-zA-Z_]*[\s\S]*?```/g, '').trim()
  return cleaned
}

export function buildRoleThread({
  nodeKey,
  isEntry,
  steps,
  tickets,
  labelOf,
  avatarOf,
}: RoleThreadInput): RoleThreadEntry[] {
  const ticketById = new Map(tickets.map((ticket) => [ticket.id, ticket]))
  const childrenByParent = new Map<string, CollabTicket[]>()
  for (const ticket of tickets) {
    if (!ticket.parent_ticket_id) continue
    const list = childrenByParent.get(ticket.parent_ticket_id) ?? []
    list.push(ticket)
    childrenByParent.set(ticket.parent_ticket_id, list)
  }

  const entries: RoleThreadEntry[] = []

  for (const step of steps) {
    if (step.role_key === nodeKey) {
      entries.push({ id: step.id, kind: 'work', sequence: step.sequence, step })
      continue
    }
    if (step.role_key === 'user') {
      if (isEntry) {
        entries.push({
          id: step.id,
          kind: 'user',
          sequence: step.sequence,
          fromLabel: '你',
          fromAvatar: '🧑',
          text: readableMessage(step.content),
        })
      }
      continue
    }
    if (step.action !== 'delegate' || step.target_key !== nodeKey) continue

    const source = step.ticket_id ? ticketById.get(step.ticket_id) : undefined
    const assigned =
      source && source.holder_key === nodeKey
        ? source
        : source
          ? (childrenByParent.get(source.id) ?? []).find((child) => child.holder_key === nodeKey)
          : undefined

    entries.push({
      id: `handoff-${step.id}`,
      kind: 'handoff',
      // 排在这一步之后、本岗位动手之前
      sequence: step.sequence + 0.5,
      fromKey: step.role_key,
      fromLabel: labelOf(step.role_key),
      fromAvatar: avatarOf(step.role_key),
      text: readableMessage(step.content),
      ticket: assigned,
    })
  }

  return entries.sort((left, right) => left.sequence - right.sequence)
}

/** 工单要点折成几行清单，供交接卡片展示（没有就不显示）。 */
export function ticketHighlights(ticket?: CollabTicket): string[] {
  if (!ticket) return []
  const brief = ticket.brief ?? {}
  const lines: string[] = []
  if (brief.goal) lines.push(`目标：${brief.goal}`)
  for (const item of brief.requirements ?? []) lines.push(`要求：${item}`)
  for (const item of brief.deliverables ?? []) lines.push(`交付：${item}`)
  for (const item of brief.acceptance ?? []) lines.push(`验收：${item}`)
  for (const item of brief.constraints ?? []) lines.push(`约束：${item}`)
  return lines
}
