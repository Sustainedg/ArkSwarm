/**
 * 协作信封事件（`collab.*`）的纯归约器：把流里的步骤、工单与运行状态折进一个视图模型。
 *
 * 与 `agentStream.ts` 分两层：协作页把 `collab.step.event` 里内嵌的**原始智能体事件**解包出来，
 * 交给同一个 `applyAgentEvent()` 归约，因此协作时间线的渲染与对话页完全一致。
 * 这里不碰 DOM、不发请求。
 */
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from './agentStream'
import type { CollabEvent, CollabRun, CollabRunStatus, CollabTicket } from './types'

/** 正在流式输出的那一步 */
export interface LiveStep {
  stepId: string
  /** 岗位 key（图上的节点） */
  nodeKey: string
  duty: string
  roleName: string
  ticketId?: string | null
  agent: AgentStreamState
}

export interface CollabNotice {
  id: number
  level: string
  text: string
  /** 有 error 级别提示时，把对应步骤记下来供「重试」用 */
  stepId?: string | null
}

export interface CollabTurnState {
  status: CollabRunStatus
  reason?: string | null
  live: LiveStep | null
  tickets: Record<string, CollabTicket>
  notices: CollabNotice[]
  nextNoticeId: number
}

/** 用运行详情初始化视图；`status` 以详情为准。 */
export function makeCollabTurn(run: CollabRun, tickets: CollabTicket[] = []): CollabTurnState {
  return {
    status: run.status,
    reason: run.error ?? null,
    live: null,
    tickets: Object.fromEntries(tickets.map((ticket) => [ticket.id, ticket])),
    notices: [],
    nextNoticeId: 1,
  }
}

/** 消费一条协作信封事件，返回新状态（不改原对象）。 */
export function applyCollabEvent(state: CollabTurnState, event: CollabEvent): CollabTurnState {
  switch (event.type) {
    case 'collab.run.started':
      return { ...state, status: 'running', reason: null }

    case 'collab.step.started':
      return {
        ...state,
        status: 'running',
        live: {
          stepId: event.step_id ?? '',
          nodeKey: event.node_key ?? '',
          duty: event.duty ?? '',
          roleName: event.role_name ?? event.node_key ?? '角色',
          ticketId: event.ticket_id ?? null,
          agent: emptyAgentStream(),
        },
      }

    case 'collab.step.event': {
      if (!state.live || !event.event || (event.step_id && event.step_id !== state.live.stepId)) {
        return state
      }
      return {
        ...state,
        live: { ...state.live, agent: applyAgentEvent(state.live.agent, event.event) },
      }
    }

    case 'collab.step.finished': {
      const failed = event.status === 'error'
      return {
        ...state,
        live: null,
        notices: failed
          ? [...state.notices, {
              id: state.nextNoticeId,
              level: 'error',
              text: event.error ?? '这一步出错了，可以修好后点「重试」',
              stepId: event.step_id ?? null,
            }].slice(-30)
          : state.notices,
        nextNoticeId: failed ? state.nextNoticeId + 1 : state.nextNoticeId,
      }
    }

    case 'collab.ticket.created':
    case 'collab.ticket.updated': {
      if (!event.ticket) return state
      return { ...state, tickets: { ...state.tickets, [event.ticket.id]: event.ticket } }
    }

    case 'collab.run.status':
      return { ...state, status: (event.status as CollabRunStatus) ?? state.status, reason: event.reason ?? null }

    case 'collab.notice': {
      const notice: CollabNotice = {
        id: state.nextNoticeId,
        level: event.level ?? 'info',
        text: event.text ?? '',
        stepId: state.live?.stepId ?? null,
      }
      return { ...state, notices: [...state.notices, notice].slice(-30), nextNoticeId: state.nextNoticeId + 1 }
    }

    default:
      return state
  }
}
