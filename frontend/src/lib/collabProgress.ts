/**
 * 协作运行页的纯视图模型：把编排图、步骤与工单折成「谁在哪一步、现在轮到谁」。
 *
 * 桌面端与移动端共用这份计算。主会话只展示用户与入口领导者；其他岗位的完整
 * 对话通过进度卡片点开查看。这里不碰 DOM、不发请求。
 */
import { ACTIONS, EDGE_KINDS } from './collabDraft'
import type { FinishedStep, LiveStep } from './collabStream'
import type {
  CollabEdgeKind,
  CollabGraph,
  CollabRunStatus,
  CollabStep,
  CollabTicket,
  Role,
} from './types'

export type CollabRoleState = 'pending' | 'active' | 'waiting' | 'error' | 'done'

export interface CollabRoleProgress {
  key: string
  label: string
  avatar: string
  duty: string
  isEntry: boolean
  state: CollabRoleState
  stateLabel: string
  stepCount: number
  summary: string
  lastAction?: string
  isLive: boolean
  activeTickets: CollabTicket[]
}

export interface CollabFlowEdge {
  id: string
  from: string
  to: string
  kind: CollabEdgeKind
  kindLabel: string
}

interface BuildProgressArgs {
  graph?: CollabGraph | null
  entryKey?: string | null
  steps: CollabStep[]
  tickets: CollabTicket[]
  roles: Role[]
  live?: LiveStep | null
  streamed?: FinishedStep[]
  runStatus: CollabRunStatus
}

const ACTIVE_TICKET_STATES = new Set(['open', 'working', 'waiting_children', 'review', 'rework', 'blocked'])

const TICKET_LABELS: Record<string, string> = {
  open: '待领取',
  working: '处理中',
  waiting_children: '等子工单',
  review: '待审查',
  rework: '返工中',
  blocked: '等你决定',
}

const ACTION_LABELS = Object.fromEntries(ACTIONS.map((item) => [item.value, item.label]))
const EDGE_LABELS = Object.fromEntries(EDGE_KINDS.map((item) => [item.value, item.label]))

/** 从入口节点做一次广度遍历，得到用户能看懂的推进顺序；孤立节点随后补上。 */
export function orderedCollabNodes(graph: CollabGraph, entryKey?: string | null) {
  const byKey = new Map(graph.nodes.map((node) => [node.key, node]))
  const order = new Map(graph.nodes.map((node, index) => [node.key, index]))
  const adjacency = new Map<string, string[]>()
  for (const edge of graph.edges) {
    const next = adjacency.get(edge.from_key) ?? []
    next.push(edge.to_key)
    adjacency.set(edge.from_key, next)
  }

  const queue = entryKey && byKey.has(entryKey) ? [entryKey] : graph.nodes.slice(0, 1).map((node) => node.key)
  const seen = new Set<string>()
  const keys: string[] = []
  while (queue.length > 0) {
    const key = queue.shift()!
    if (seen.has(key) || !byKey.has(key)) continue
    seen.add(key)
    keys.push(key)
    const next = (adjacency.get(key) ?? []).slice().sort((left, right) => (order.get(left) ?? 0) - (order.get(right) ?? 0))
    queue.push(...next)
  }
  for (const node of graph.nodes) {
    if (!seen.has(node.key)) keys.push(node.key)
  }
  return keys.map((key) => byKey.get(key)!).filter(Boolean)
}

function latestStep(steps: CollabStep[]): CollabStep | undefined {
  return steps.length > 0 ? steps[steps.length - 1] : undefined
}

function snippet(text: string | null | undefined, limit = 72): string {
  const oneLine = (text ?? '').replace(/\s+/g, ' ').trim()
  return oneLine.length > limit ? `${oneLine.slice(0, limit)}…` : oneLine
}

/** 已落库的一步是否已经有可展示的产出（正文或结构化部件）。 */
export function stepHasOutput(step: { content?: string | null; parts?: unknown[] | null }): boolean {
  return Boolean((step.content ?? '').trim()) || (step.parts?.length ?? 0) > 0
}

/**
 * 一步还没产出时，给出一句人话说明它当前的状态。
 *
 * 关键场景：角色正在跑（`streaming`）但输出只在跑完后才落库，此时点开抽屉若渲染空气泡，
 * 用户会以为「显示处理中却什么都没发生」；中断/出错也要说清楚，避免看起来像卡死。
 */
export function stepStatusHint(status: string, hasOutput: boolean): string | null {
  if (hasOutput) return null
  if (status === 'streaming') return '正在处理中……这一步的输出会在跑完后显示。'
  if (status === 'interrupted') return '这一步被中断，没有留下输出；可在顶栏点「继续」让它重跑。'
  if (status === 'error') return '这一步出错了，没有产出。'
  if (status === 'cancelled') return '这一步被取消。'
  return '这一步没有可展示的输出。'
}

/** 计算一张协作图在本次运行里的进度。 */
export function buildCollabProgress({
  graph,
  entryKey,
  steps,
  tickets,
  roles,
  live,
  streamed = [],
  runStatus,
}: BuildProgressArgs): { nodes: CollabRoleProgress[]; edges: CollabFlowEdge[] } {
  if (!graph || graph.nodes.length === 0) return { nodes: [], edges: [] }

  const roleById = new Map(roles.map((role) => [role.id, role]))
  const nodes = orderedCollabNodes(graph, entryKey).map((node): CollabRoleProgress => {
    const ownSteps = steps.filter((step) => step.role_key === node.key)
    const persistedIds = new Set(ownSteps.map((step) => step.id))
    const ownStreamed = streamed.filter(
      (step) => step.nodeKey === node.key && !persistedIds.has(step.stepId),
    )
    const last = latestStep(ownSteps)
    const lastStreamed = ownStreamed.length > 0 ? ownStreamed[ownStreamed.length - 1] : undefined
    const stepCount = ownSteps.length + ownStreamed.length
    const activeTickets = tickets.filter(
      (ticket) => ticket.holder_key === node.key && ACTIVE_TICKET_STATES.has(ticket.state),
    )
    const isLive = live?.nodeKey === node.key
    const blockedTicket = activeTickets.find((ticket) => ticket.state === 'blocked')
    const activeTicket = blockedTicket ?? activeTickets[0]
    // 运行已不在进行中，但这一步还停在 streaming / 被中断：说明它没跑完（例如服务重启）。
    // 否则界面会一直显示「处理中」，点开却看不到任何输出。
    const stalled =
      !isLive && runStatus !== 'running' && (last?.status === 'streaming' || last?.status === 'interrupted')

    let state: CollabRoleState = 'pending'
    let stateLabel = '未开始'
    if (isLive) {
      state = 'active'
      stateLabel = '实时输出中'
    } else if (blockedTicket) {
      state = 'waiting'
      stateLabel = '等你决定'
    } else if (stalled) {
      state = 'error'
      stateLabel = last?.status === 'interrupted' ? '已中断' : '已停止'
    } else if (activeTicket) {
      state = 'active'
      stateLabel = TICKET_LABELS[activeTicket.state] ?? '进行中'
    } else if (last?.status === 'error' || lastStreamed?.status === 'error') {
      state = 'error'
      stateLabel = '需处理'
    } else if (stepCount > 0 && runStatus === 'done') {
      state = 'done'
      stateLabel = '已完成'
    } else if (stepCount > 0) {
      state = 'done'
      stateLabel = '已参与'
    }

    const summary = isLive
      ? '正在生成实时输出，点开可查看'
      : lastStreamed?.agent.error ||
        snippet(lastStreamed?.agent.text) ||
        last?.error ||
        last?.gist ||
        (last?.status === 'streaming' ? '正在处理中，点开可查看' : activeTicket?.title) ||
        (stepCount > 0 ? '本轮工作已落库' : '尚未参与本次协作')

    return {
      key: node.key,
      label: node.label || node.key,
      avatar: roleById.get(node.role_id)?.avatar ?? '🤖',
      duty: node.duty,
      isEntry: node.key === entryKey,
      state,
      stateLabel,
      stepCount,
      summary,
      lastAction: lastStreamed?.action
        ? ACTION_LABELS[lastStreamed.action] ?? lastStreamed.action
        : last?.action
          ? ACTION_LABELS[last.action] ?? last.action
          : undefined,
      isLive,
      activeTickets,
    }
  })

  const labels = new Map(nodes.map((node) => [node.key, node.label]))
  const edges = graph.edges.map((edge) => ({
    id: edge.id,
    from: labels.get(edge.from_key) ?? edge.from_key,
    to: labels.get(edge.to_key) ?? edge.to_key,
    kind: edge.kind,
    kindLabel: EDGE_LABELS[edge.kind] ?? edge.kind,
  }))
  return { nodes, edges }
}
