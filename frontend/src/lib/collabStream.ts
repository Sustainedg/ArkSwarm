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
  agentName: string
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
  /**
   * 正在流式输出的步骤（波次并行时可能同时有多个；串行时最多一个）。
   * 每个条目带自己的 stepId，SSE 事件按它分流，交织不会张冠李戴。
   */
  live: LiveStep[]
  /** 本轮 SSE 已完成的步骤，详情刷新后清空，避免和落库步骤重复。 */
  completed: FinishedStep[]
  tickets: Record<string, CollabTicket>
  notices: CollabNotice[]
  nextNoticeId: number
}

/** 兼容消费方的单数视角：数组里最新的那一步（串行场景它就是唯一的）。 */
export function latestLive(live: LiveStep[]): LiveStep | null {
  return live.length > 0 ? live[live.length - 1] : null
}

/** 某个岗位正在流式输出的那一步（岗位抽屉 / 进度条用它）。 */
export function liveOfNode(live: LiveStep[], nodeKey: string | null | undefined): LiveStep | null {
  if (!nodeKey) return null
  return live.find((step) => step.nodeKey === nodeKey) ?? null
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
  const lastByNode = new Map<string, CollabStep>()
  for (const step of steps) lastByNode.set(step.role_key, step)
  const notices: CollabNotice[] = []
  for (const step of steps) {
    if (lastByNode.get(step.role_key)?.id !== step.id) continue
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
    live: [],
    completed: [],
    tickets: Object.fromEntries(tickets.map((ticket) => [ticket.id, ticket])),
    notices,
    nextNoticeId: notices.length + 1,
  }
}

/**
 * 用运行详情对齐视图，但**保住本端还在收的流式状态**。
 *
 * 运行详情每 3~4 秒轮询一次。如果每次都把 `live` / `completed` 清空，正在流式输出的
 * 那一步会瞬间退化成落库时「status=streaming、内容还是空的」那一行 —— 岗位抽屉里就只剩
 * 「正在处理中……这一步的输出会在跑完后显示」，明明本端已经收到了逐字内容。
 *
 * 因此这里按步骤 id 对齐：
 * - 详情里仍标 `streaming`（或详情快照还没包含它）的当前步骤，保留 `live`；
 * - 已结束但落库行还没写完的步骤，保留在 `completed`（线程会优先显示流式内容）；
 * - 真正落库完成的步骤交给 `steps`，避免与 `completed` 重复。
 */
export function syncTurnWithRun(
  prev: CollabTurnState | null,
  run: CollabRun,
  tickets: CollabTicket[] = [],
  steps: CollabStep[] = [],
  dismissed: ReadonlySet<string> = new Set(),
): CollabTurnState {
  const base = prev ?? makeCollabTurn(run, tickets, steps, dismissed)
  const rows = new Map(steps.map((step) => [step.id, step]))
  /** 运行已不在推进时，任何残留的 streaming 行都不再算「本端正在收」。 */
  const stillLive = (stepId: string) => {
    if (run.status !== 'running') return false
    const row = rows.get(stepId)
    // 详情快照还没包含这一步（刚创建）时，以运行状态为准。
    return row ? row.status === 'streaming' : true
  }
  return {
    ...base,
    status: run.status,
    reason: run.error ?? null,
    live: base.live.filter((step) => stillLive(step.stepId)),
    completed: base.completed.filter((step) => {
      const row = rows.get(step.stepId)
      return !row || row.status === 'streaming'
    }),
    tickets: Object.fromEntries(tickets.map((ticket) => [ticket.id, ticket])),
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

    case 'collab.step.started': {
      const started: LiveStep = {
        stepId: event.step_id ?? '',
        nodeKey: event.node_key ?? '',
        duty: event.duty ?? '',
        agentName: event.agent_name ?? event.node_key ?? '智能体',
        ticketId: event.ticket_id ?? null,
        agent: emptyAgentStream(),
      }
      return {
        ...state,
        status: 'running',
        // 这一轮开始推进了：上一轮的临时提醒（审计、诊断）自动退休；该岗位上次的失败提醒也一并退休
        // —— 它要么正在被重跑，要么已经被重新派活覆盖。
        notices: retireNodeNotices(
          pruneNotices(state.notices.filter((notice) => Boolean(notice.stepId))),
          event.node_key,
        ),
        // 并行波次里多个步骤同时在跑：按 stepId 各自登记，不互相顶掉
        live: [...state.live.filter((step) => step.stepId !== started.stepId), started],
      }
    }

    case 'collab.step.event': {
      if (!event.event || !event.step_id) return state
      const index = state.live.findIndex((step) => step.stepId === event.step_id)
      if (index < 0) return state
      const updated = [...state.live]
      updated[index] = { ...updated[index], agent: applyAgentEvent(updated[index].agent, event.event) }
      return { ...state, live: updated }
    }

    case 'collab.step.finished': {
      const failed = event.status === 'error'
      const index = state.live.findIndex((step) => step.stepId === event.step_id)
      // 断线重订阅会重放本轮事件：找不到对应 live（已收过账）就什么都不做
      if (index < 0) return state
      const finishedStep = state.live[index]
      const nodeKey = finishedStep.nodeKey ?? null
      // 同一个 stepId 只保留最新一份，避免抽屉里出现重复气泡。
      const completed = [
        ...state.completed.filter((step) => step.stepId !== finishedStep.stepId),
        {
          ...finishedStep,
          status: event.status ?? 'complete',
          action: event.action ?? '',
          error: event.error ?? finishedStep.agent.error,
        },
      ].slice(-60)
      const live = state.live.filter((step) => step.stepId !== finishedStep.stepId)
      const kept = retireNodeNotices(state.notices, nodeKey)
      if (!failed) {
        // 这一步跑通了：它自己以及同一岗位之前那条失败提醒都已经过时
        return { ...state, live, completed, notices: kept }
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
        live,
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
        // 收尾时不再有「正在跑」的步骤；残留（如挂起前的 streaming）一并清掉
        live: ended ? [] : state.live,
        notices: ended
          ? state.notices.filter((notice) => notice.level === 'ask')
          : state.notices,
      }
    }

    case 'collab.notice': {
      const text = event.text ?? ''
      const level = event.level ?? 'info'
      // 提醒挂在「此刻最新在跑的那一步」上；并行波次里各步骤的 notice 已带 step_id 定位，
      // 没带时归到最新的 live（与串行时代的归属一致）。
      const nodeKey = latestLive(state.live)?.nodeKey ?? null
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
