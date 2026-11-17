/** 入口智能体聊天事件（`collab.chat.*`）的纯归约器，与工单推进状态互不干扰。 */
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from './agentStream'
import { normalizeCollabChoices, type CollabAsk } from './collabAsk'
import type { CollabChatMessage, CollabChoice, CollabEvent, CollabStep } from './types'

export interface LiveChatReply {
  messageId: string
  roleKey: string
  agentName: string
  agent: AgentStreamState
}

export interface ChatTurnState {
  live: LiveChatReply | null
  /** 最近一次聊天结束后的提示：需求已同步、解析告警或执行错误。 */
  notice: { level: 'info' | 'warn' | 'error'; text: string } | null
  /**
   * CEO 判断「这条消息需要推进任务」时给出的决策卡（聊天结束后渲染）。
   * 来自 chat_finished 事件的 advance 字段，或刷新后从落库 meta 里恢复；
   * 用户点选 / 关闭 / 下一轮聊天开始时清空。
   */
  advance: CollabAsk | null
}

export function emptyChatTurn(): ChatTurnState {
  return { live: null, notice: null, advance: null }
}

/** 用户点掉聊天里那条提示（需求已同步 / 解析告警 / 执行错误）：它是一次性的。 */
export function dismissChatNotice(state: ChatTurnState): ChatTurnState {
  return { ...state, notice: null }
}

/** 用户点选或关掉「推进任务」决策卡：这张卡是一次性的。 */
export function dismissChatAdvance(state: ChatTurnState): ChatTurnState {
  return { ...state, advance: null }
}

/** 把 chat_finished 的 advance（或落库 meta 里恢复的同构数据）规整成决策卡。 */
export function chatAdvanceFrom(raw: unknown, messageId = ''): CollabAsk | null {
  if (!raw || typeof raw !== 'object') return null
  const record = raw as Record<string, unknown>
  const order = typeof record.order === 'string' ? record.order.trim() : ''
  const reason = typeof record.reason === 'string' ? record.reason.trim() : ''
  const options = normalizeCollabChoices(record.options)
  if (!order && !reason && options.length === 0) return null
  const question = reason || order || 'CEO 判断这条消息需要推进任务'
  const choices: CollabChoice[] = options.length > 0
    ? options
    : [
        { label: '按建议推进', value: order, freeform: false },
        { label: '我再想想', value: '', freeform: true },
      ]
  return { ticketId: '', messageId, question, options: choices }
}

/** 后端写的是 UTC 的 naive 时间（`2026-11-16 15:03:28.184362`）：补上 T/Z 再解析。 */
function collabTimeMs(value: string | null | undefined): number | null {
  if (!value) return null
  const normalized = value.includes('T') ? value : value.replace(' ', 'T')
  const ms = Date.parse(normalized.endsWith('Z') ? normalized : `${normalized}Z`)
  return Number.isFinite(ms) ? ms : null
}

/**
 * 这条 CEO 聊天里的推进卡是否已经被之后的用户指令消费掉了。
 *
 * 工单通道的用户消息不在 `chat_messages` 里，而是 `collab_steps` 的 `role_key=user`；
 * 只要有这样一条消息晚于 CEO 的回复，卡上的 `order` 就已经发出去执行了。旧数据里
 * `meta.advance` 不会自动消失，这个判断保证刷新/换设备后不会把旧卡重新弹出来。
 */
export function advanceOvertakenByTicketSteps(
  chat: CollabChatMessage,
  steps: CollabStep[],
): boolean {
  const repliedAt = collabTimeMs(chat.finished_at ?? chat.created_at)
  if (repliedAt === null) return false
  let latestUserStep: number | null = null
  for (const step of steps) {
    if (step.role_key !== 'user') continue
    const at = collabTimeMs(step.started_at)
    if (at !== null && (latestUserStep === null || at > latestUserStep)) latestUserStep = at
  }
  return latestUserStep !== null && latestUserStep > repliedAt
}

export function applyChatEvent(state: ChatTurnState, event: CollabEvent): ChatTurnState {
  switch (event.type) {
    case 'collab.chat.started':
      return {
        ...state,
        notice: null,
        advance: null,
        live: {
          messageId: event.message_id ?? '',
          roleKey: event.role_key ?? '',
          agentName: event.agent_name ?? '领导者',
          agent: emptyAgentStream(),
        },
      }

    case 'collab.chat.event': {
      if (!state.live || !event.event || (event.message_id && event.message_id !== state.live.messageId)) {
        return state
      }
      return { ...state, live: { ...state.live, agent: applyAgentEvent(state.live.agent, event.event) } }
    }

    case 'collab.chat.finished': {
      const applied = event.applied
      let text = event.warning ?? ''
      let level: 'info' | 'warn' | 'error' = text ? 'warn' : 'info'
      if (event.status === 'error') {
        text = event.error ?? 'CEO 这次回复失败了，可以再试一次'
        level = 'error'
      } else if (event.status === 'cancelled') {
        text = event.error ?? '这条 CEO 回复已停止'
        level = 'warn'
      } else if (applied?.ticket_count) {
        const fields = applied.fields && applied.fields.length > 0
          ? `（${applied.fields.join('、')}）`
          : ''
        const line = `已把新要求同步到 ${applied.ticket_count} 张进行中的工单${fields}，下一轮步骤生效`
        text = text ? `${text}；${line}` : line
        level = text === line ? 'info' : 'warn'
      }
      const advance = event.advance ?? null
      if (advance && event.status === 'complete' && !event.error) {
        const line = 'CEO 判断这条消息需要推进任务：点下面的选项确认后才进入工单通道'
        text = text ? `${text}；${line}` : line
        level = 'warn'
      }
      return { live: null, notice: text ? { level, text } : null, advance: chatAdvanceFrom(advance, event.message_id ?? '') }
    }

    default:
      return state
  }
}
