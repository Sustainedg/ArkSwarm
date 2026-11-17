/**
 * 协作「等待你决定」的纯视图逻辑：把工单里的 question/options 规整成决策卡。
 *
 * 旧版本只落库 question（选项写在正文里），这里会补一张兼容卡片：
 * 提到「默认方案」时给「按你的默认方案来」，并始终保留自由填写入口。
 * 本模块不碰 DOM、不发请求，桌面端与移动端共用。
 */
import type { CollabChoice, CollabTicket } from './types'

export interface CollabAsk {
  ticketId: string
  /** 「推进任务」卡的身份：产生它的 CEO 聊天消息 ID（用于记住已点选/已关闭）。 */
  messageId?: string
  question: string
  options: CollabChoice[]
}

export function normalizeCollabChoices(raw: unknown): CollabChoice[] {
  if (!Array.isArray(raw)) return []
  const options: CollabChoice[] = []
  for (const item of raw) {
    if (typeof item === 'string') {
      const label = item.trim()
      if (label) options.push({ label, value: label, freeform: false })
      continue
    }
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const label = typeof record.label === 'string' ? record.label.trim() : ''
    const value = typeof record.value === 'string' ? record.value.trim() : ''
    if (!label && !value) continue
    const description = typeof record.description === 'string' ? record.description.trim() : ''
    options.push({
      label: label || value,
      value: value || label,
      description: description || null,
      freeform: record.freeform === true,
    })
  }
  return options
}

/** 没有结构化选项时给出可用的兜底，保证旧记录也能推动，而不是只剩一条长文本。 */
export function collabAskOptions(question: string, raw: unknown): CollabChoice[] {
  const options = normalizeCollabChoices(raw)
  if (options.length > 0) return options
  const fallback: CollabChoice[] = []
  if (question.includes('默认')) {
    fallback.push({
      label: '按你的默认方案来',
      value: '按你的默认方案来',
      description: '让入口智能体按它给出的默认方案继续。',
    })
  }
  fallback.push({ label: '我来补充说明', value: '', description: '在下方输入你的答复。', freeform: true })
  return fallback
}

export function findPendingCollabAsks(tickets: CollabTicket[]): CollabAsk[] {
  const asks: CollabAsk[] = []
  for (const ticket of tickets) {
    if (ticket.state !== 'blocked') continue
    const result = (ticket.result ?? {}) as Record<string, unknown>
    const question = typeof result.question === 'string' ? result.question.trim() : ''
    if (!question) continue
    asks.push({ ticketId: ticket.id, question, options: collabAskOptions(question, result.options) })
  }
  return asks
}

export function collabChoiceAnswer(choice: CollabChoice): string {
  return (choice.value || choice.label || '').trim()
}
