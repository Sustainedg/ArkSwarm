/** 入口角色聊天事件（`collab.chat.*`）的纯归约器，与工单推进状态互不干扰。 */
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from './agentStream'
import type { CollabEvent } from './types'

export interface LiveChatReply {
  messageId: string
  roleKey: string
  roleName: string
  agent: AgentStreamState
}

export interface ChatTurnState {
  live: LiveChatReply | null
  /** 最近一次聊天结束后的提示：需求已同步、解析告警或执行错误。 */
  notice: { level: 'info' | 'warn' | 'error'; text: string } | null
}

export function emptyChatTurn(): ChatTurnState {
  return { live: null, notice: null }
}

/** 用户点掉聊天里那条提示（需求已同步 / 解析告警 / 执行错误）：它是一次性的。 */
export function dismissChatNotice(state: ChatTurnState): ChatTurnState {
  return { ...state, notice: null }
}

export function applyChatEvent(state: ChatTurnState, event: CollabEvent): ChatTurnState {
  switch (event.type) {
    case 'collab.chat.started':
      return {
        ...state,
        notice: null,
        live: {
          messageId: event.message_id ?? '',
          roleKey: event.role_key ?? '',
          roleName: event.role_name ?? 'CEO',
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
      return { live: null, notice: text ? { level, text } : null }
    }

    default:
      return state
  }
}
