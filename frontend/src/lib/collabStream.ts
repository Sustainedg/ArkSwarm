/**
 * 协作信封事件（`collab.*`）的纯归约器：把流里的步骤、工单与运行状态折进一个视图模型。
 *
 * 与 `agentStream.ts` 分两层：协作页把 `collab.step.event` 里内嵌的**原始智能体事件**解包出来，
 * 交给同一个 `applyAgentEvent()` 归约，因此协作时间线的渲染与对话页完全一致。
 * 这里不碰 DOM、不发请求。
 */
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from './agentStream'
import type { CollabEvent, CollabRun, CollabRunStatus, CollabStep, CollabTicket } from './types'

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

/** 已经结束但还没随运行详情落回界面的步骤；用于在整轮推进期间保持会话连续。 */
export interface FinishedStep extends LiveStep {
  status: string
  action: string
  error?: string | null
}

export interface CollabNotice {
  id: number
  /** 稳定身份：失败步骤看 stepId，其余看「岗位 + 文本」；用于去重与「已关闭」记忆。 */
  key: string
  level: string
  text: string
  /** 这条提醒属于哪个岗位：该岗位后来跑成功（或被重跑）时，它就过时了。 */
  nodeKey?: string | null
  /** 有 error 级别提示时，把对应步骤记下来供「重试」用 */
  stepId?: string | null
}

/** 一条提醒的身份：失败步骤用 stepId（重试后能精确退休），其余用「岗位 + 文本」。 */
export function noticeKey(notice: {
  level: string
  text: string
  stepId?: string | null
  nodeKey?: string | null
}): string {
  return notice.stepId
    ? `step:${notice.stepId}`
    : `${notice.nodeKey ?? 'run'}:${notice.level}:${notice.text}`
}

/** 同一个身份只留最后一条（后出现的更接近现状），顺序不变。 */
export function pruneNotices(notices: CollabNotice[]): CollabNotice[] {
  const lastByKey = new Map<string, number>()
  notices.forEach((notice, index) => lastByKey.set(notice.key, index))
  return notices.filter((notice, index) => lastByKey.get(notice.key) === index)
}

export interface CollabTurnState {
  status: CollabRunStatus
  reason?: string | null
  live: LiveStep | null
  /** 本轮 SSE 已完成的步骤，详情刷新后清空，避免和落库步骤重复。 */
  completed: FinishedStep[]
  tickets: Record<string, CollabTicket>
  notices: CollabNotice[]
  nextNoticeId: number
}

/**
 * 刷新后重建「待处理」提醒：只留**现在还需要你处理**的失败步骤。
 *
 * 三条退休规则（对应「过时提醒一直挂在对话里」的真实反馈）：
 *
 * 1. 运行已收尾（``done`` / ``cancelled``）：失败已成历史，时间线里查得到，不再占着待处理区；
 * 2. 每个岗位只看它**最后一次**参与的结果：后来跑成功了，早先那次失败就被顶替，
 *    「重试这一步」也没有意义了；
 * 3. 同一岗位重复报同一条错：只留最后一条（重试指向最新的那一步），避免同一条提醒刷屏。
 */
function staleFailureNotices(
  run: CollabRun,
  steps: CollabStep[],
  dismissed: ReadonlySet<string>,
): CollabNotice[] {
  if (run.status === 'done' || run.status === 'cancelled') return []
  const lastByRole = new Map<string, CollabStep>()
  for (const step of steps) lastByRole.set(step.role_key, step)
  const notices: CollabNotice[] = []
  for (const step of steps) {
    if (lastByRole.get(step.role_key)?.id !== step.id) continue
    if (step.status !== 'error') continue
    const key = `step:${step.id}`
    if (dismissed.has(key)) continue
    notices.push({
      id: notices.length + 1,
      key,
      level: 'error',
      text: step.error || '这一步出错了，可以修好后点「重试」',
      nodeKey: step.role_key,
      stepId: step.id,
    })
  }
  return notices.slice(-10)
}

/** 用运行详情初始化视图；`status` 以详情为准。 */
export function makeCollabTurn(
  run: CollabRun,
  tickets: CollabTicket[] = [],
  steps: CollabStep[] = [],
  dismissed: ReadonlySet<string> = new Set(),
): CollabTurnState {
  const notices = staleFailureNotices(run, steps, dismissed)
  return {
    status: run.status,
    reason: run.error ?? null,
    live: null,
    completed: [],
    tickets: Object.fromEntries(tickets.map((ticket) => [ticket.id, ticket])),
    notices,
    nextNoticeId: notices.length + 1,
  }
}

/** 用户点了提醒上的「×」：这条等它下次真的又出现再说。 */
export function dismissCollabNotice(state: CollabTurnState, key: string): CollabTurnState {
  return { ...state, notices: state.notices.filter((notice) => notice.key !== key) }
}

/** 退休「这个岗位相关的」提醒：它要么被重跑了，要么已经解决。 */
function retireNodeNotices(notices: CollabNotice[], nodeKey?: string | null): CollabNotice[] {
  if (!nodeKey) return notices
  return notices.filter((notice) => notice.nodeKey !== nodeKey)
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
        // 这一轮开始推进了：上一轮的临时提醒（审计、诊断）自动退休；该岗位上次的失败提醒也一并退休
        // —— 它要么正在被重跑，要么已经被重新派活覆盖。
        notices: retireNodeNotices(
          pruneNotices(state.notices.filter((notice) => Boolean(notice.stepId))),
          event.node_key,
        ),
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
      const nodeKey = state.live?.nodeKey ?? null
      const completed = state.live
        ? [
            ...state.completed,
            {
              ...state.live,
              status: event.status ?? 'complete',
              action: event.action ?? '',
              error: event.error ?? state.live.agent.error,
            },
          ].slice(-60)
        : state.completed
      const kept = retireNodeNotices(state.notices, nodeKey)
      if (!failed) {
        // 这一步跑通了：它自己以及同一岗位之前那条失败提醒都已经过时
        return { ...state, live: null, completed, notices: kept }
      }
      const notice: CollabNotice = {
        id: state.nextNoticeId,
        key: `step:${event.step_id ?? ''}`,
        level: 'error',
        text: event.error ?? '这一步出错了，可以修好后点「重试」',
        nodeKey,
        stepId: event.step_id ?? null,
      }
      return {
        ...state,
        live: null,
        completed,
        notices: pruneNotices([...kept, notice]).slice(-30),
        nextNoticeId: state.nextNoticeId + 1,
      }
    }

    case 'collab.ticket.created':
    case 'collab.ticket.updated': {
      if (!event.ticket) return state
      return { ...state, tickets: { ...state.tickets, [event.ticket.id]: event.ticket } }
    }

    case 'collab.run.status': {
      const status = (event.status as CollabRunStatus) ?? state.status
      // 运行收尾了：待处理的失败提醒就该退场（详情刷新后也不会再重建，见 staleFailureNotices）。
      const ended = status === 'done' || status === 'cancelled'
      return {
        ...state,
        status,
        reason: event.reason ?? null,
        notices: ended
          ? state.notices.filter((notice) => notice.level === 'ask')
          : state.notices,
      }
    }

    case 'collab.notice': {
      const text = event.text ?? ''
      const level = event.level ?? 'info'
      const nodeKey = state.live?.nodeKey ?? null
      const notice: CollabNotice = {
        id: state.nextNoticeId,
        key: noticeKey({ level, text, nodeKey }),
        level,
        text,
        nodeKey,
        // 只有「这一步失败了」的提醒才配「重试」，普通提示不挂重试按钮
        stepId: null,
      }
      return {
        ...state,
        notices: pruneNotices([...state.notices, notice]).slice(-30),
        nextNoticeId: state.nextNoticeId + 1,
      }
    }

    default:
      return state
  }
}
