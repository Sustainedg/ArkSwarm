/**
 * 协作编排的**共享词汇表**：岗位 / 连线 / 动作的取值与中文标签，以及「草稿 ⇄ 提交载荷」的纯函数。
 *
 * 桌面端是画布连线（`components/CollabCanvas` + `NodeInspector`），移动端是表单式编辑
 * （`mobile/pages/MobileCollabTeamPage`）—— 两端的字段语义、默认值与提示文案必须一致，
 * 所以统一放在这里；后端规则（key 字符、预算下限）也只在这一处镜像。
 *
 * 请求体/响应体的类型仍是 `lib/types.ts` 里与后端 `schemas.py` 对齐的那些（本模块只负责组装），
 * `TeamDraft` 则是**编辑器自己的状态**（`workspace_dir` 用空串、`entry_key` 用 null），不对外发送。
 */
import type {
  CollabBudget,
  CollabDuty,
  CollabEdge,
  CollabEdgeKind,
  CollabEdgePort,
  CollabIssue,
  CollabNode,
  CollabTeam,
  CollabTeamUpdate,
  CollabTicketAction,
  CollabTicketState,
  CollabValidation,
  CollabWorkspaceScope,
  AgentProfile,
} from './types'

/** 编辑中的一张完整协作图（保存前只存在于本地）。 */
export interface TeamDraft {
  name: string
  description: string
  entry_key: string | null
  workspace_dir: string
  budget: CollabBudget
  nodes: CollabNode[]
  edges: CollabEdge[]
}

export const DUTIES: { value: CollabDuty; label: string; hint: string }[] = [
  { value: 'coordinator', label: '协调者', hint: '与用户沟通、拆解与派活，默认拿不到工作目录' },
  { value: 'engineer', label: '实施者', hint: '动手改代码，默认对共享工作目录可写' },
  { value: 'reviewer', label: '审查者', hint: '只读检查产出，默认只能给审查意见' },
]

export const EDGE_KINDS: { value: CollabEdgeKind; label: string; hint: string }[] = [
  { value: 'assign', label: 'assign · 派发', hint: '把工单交给下游（handoff 转交 / split 拆子工单）' },
  { value: 'review', label: 'review · 审查', hint: '提交给审查者；有 review 边就必须有 rework 回边' },
  { value: 'rework', label: 'rework · 打回', hint: '审查不通过时退回，可退给实施者，也可交回协调者重新拆解' },
]

export const SCOPES: { value: CollabWorkspaceScope | ''; label: string }[] = [
  { value: '', label: '按岗位取默认' },
  { value: 'none', label: '不给工作目录' },
  { value: 'read', label: '只读' },
  { value: 'write', label: '可写' },
]

export const ACTIONS: { value: CollabTicketAction; label: string }[] = [
  { value: 'delegate', label: 'delegate 派发' },
  { value: 'submit', label: 'submit 提交' },
  { value: 'review', label: 'review 审查' },
  { value: 'done', label: 'done 确认完成' },
  { value: 'escalate', label: 'escalate 上报' },
  { value: 'ask_user', label: 'ask_user 提问' },
  { value: 'reject', label: 'reject 否决' },
]

/**
 * 工单状态的显示词汇（顶栏、进度卡、工单看板共用这一份）：`pill` 是 `.pill` 的修饰类。
 * 状态取值与流转规则在后端 `collab/schemas.py:TicketState`，这里只解决「怎么显示」。
 */
export const TICKET_STATES: { value: CollabTicketState; label: string; pill: string }[] = [
  { value: 'open', label: '待领取', pill: '' },
  { value: 'working', label: '处理中', pill: 'brand' },
  { value: 'waiting_children', label: '等子工单', pill: '' },
  { value: 'review', label: '待审查', pill: 'ok' },
  { value: 'rework', label: '返工中', pill: 'warn' },
  { value: 'escalated', label: '已上报', pill: 'warn' },
  { value: 'done', label: '已完成', pill: 'ok' },
  { value: 'rejected', label: '被否决', pill: 'err' },
  { value: 'blocked', label: '等你决定', pill: 'warn' },
]

/** 状态取值 → 显示信息；未登记的状态回落成原样文字，不吞掉信息。 */
export function ticketStateMeta(state: string): { label: string; pill: string } {
  const found = TICKET_STATES.find((item) => item.value === state)
  return found ? { label: found.label, pill: found.pill } : { label: state, pill: '' }
}

export const DEFAULT_BUDGET: CollabBudget = {
  max_steps: 200,
  max_tickets: 30,
  max_rounds_per_ticket: 3,
  max_tokens: 0,
  timeout_seconds: 3600,
}

/** 后端 BudgetSpec 有 ge 约束：这里按同一套下限夹紧，避免保存时才 422 */
export function clampBudget(budget: CollabBudget): CollabBudget {
  return {
    max_steps: Math.max(1, budget.max_steps),
    max_tickets: Math.max(1, budget.max_tickets),
    max_rounds_per_ticket: Math.max(1, budget.max_rounds_per_ticket),
    max_tokens: Math.max(0, budget.max_tokens),
    timeout_seconds: Math.max(30, budget.timeout_seconds),
  }
}

export function toTeamDraft(team: CollabTeam): TeamDraft {
  return {
    name: team.name,
    description: team.description ?? '',
    entry_key: team.entry_key ?? null,
    workspace_dir: team.workspace_dir ?? '',
    budget: { ...DEFAULT_BUDGET, ...team.budget },
    nodes: team.graph.nodes.map((node) => ({ ...node })),
    // 旧数据里没有 max_rounds，补 0（前端不依赖后端一定回传新字段）
    edges: team.graph.edges.map((edge) => ({ ...edge, max_rounds: edge.max_rounds ?? 0 })),
  }
}

/** 把服务端的 errors / warnings 合成一份画布可用的列表，并保留严重级别。 */
export function issuesFromValidation(validation: CollabValidation): CollabIssue[] {
  return [
    ...validation.errors.map((issue) => ({ ...issue, severity: 'error' as const })),
    ...validation.warnings.map((issue) => ({ ...issue, severity: 'warning' as const })),
  ]
}

/** 旧接口可能没有 severity；兼容历史 ``warn.*`` code。 */
export function isWarningIssue(issue: CollabIssue): boolean {
  return issue.severity === 'warning' || issue.code.startsWith('warn')
}

/** 草稿 → 保存载荷：名称兜底、预算夹紧、空工作目录转 null。 */
export function collabTeamPayload(draft: TeamDraft): CollabTeamUpdate {
  return {
    name: draft.name.trim() || '未命名协作组',
    description: draft.description,
    entry_key: draft.entry_key,
    workspace_dir: draft.workspace_dir.trim() || null,
    graph: { nodes: draft.nodes, edges: draft.edges },
    budget: clampBudget(draft.budget),
  }
}

/** 节点 key 必须满足后端的 `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` */
export function sanitizeKey(raw: string): string {
  const cleaned = raw.replace(/[^a-zA-Z0-9_-]/g, '-').replace(/^[^a-zA-Z0-9]+/, '')
  return cleaned.slice(0, 64)
}

export function defaultNodeKey(duty: CollabDuty, taken: Set<string>): string {
  const base = duty === 'reviewer' ? 'review' : duty === 'coordinator' ? 'lead' : 'dev'
  if (!taken.has(base)) return base
  let index = 2
  while (taken.has(`${base}-${index}`)) index += 1
  return `${base}-${index}`
}

export function newEdgeId(): string {
  return `e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`
}

/** 按两端岗位推断新连线的类型：交付给审查者 → review，审查者交出 → rework，其余 → assign。 */
export function inferEdgeKind(from?: CollabNode, to?: CollabNode): CollabEdgeKind {
  return from?.duty === 'reviewer' ? 'rework' : to?.duty === 'reviewer' ? 'review' : 'assign'
}

/** 新增一个岗位节点；第一个节点默认兼任入口（协调者）。 */
export function addNodeToDraft(draft: TeamDraft, agent: AgentProfile, at?: { x: number; y: number }): TeamDraft {
  const taken = new Set(draft.nodes.map((node) => node.key))
  const index = draft.nodes.length
  const duty: CollabDuty = index === 0 ? 'coordinator' : 'engineer'
  const node: CollabNode = {
    key: defaultNodeKey(duty, taken),
    agent_profile_id: agent.id,
    label: agent.name,
    duty,
    instructions: '',
    capability_scope: '',
    workspace_scope: null,
    output_contract: [],
    max_rounds: 0,
    x: at?.x ?? 40 + (index % 4) * 230,
    y: at?.y ?? 40 + Math.floor(index / 4) * 150,
  }
  const entry_key = draft.entry_key ?? (draft.nodes.length === 0 ? node.key : null)
  return { ...draft, nodes: [...draft.nodes, node], entry_key }
}

/** 改节点 key：入口标记与所有连线一并跟着改。 */
export function renameNodeInDraft(draft: TeamDraft, oldKey: string, rawKey: string): TeamDraft {
  const key = sanitizeKey(rawKey)
  if (!key || key === oldKey) return draft
  return {
    ...draft,
    entry_key: draft.entry_key === oldKey ? key : draft.entry_key,
    nodes: draft.nodes.map((node) => (node.key === oldKey ? { ...node, key } : node)),
    edges: draft.edges.map((edge) => ({
      ...edge,
      from_key: edge.from_key === oldKey ? key : edge.from_key,
      to_key: edge.to_key === oldKey ? key : edge.to_key,
    })),
  }
}

/** 删节点：入口标记与相关连线一并清理。 */
export function removeNodeFromDraft(draft: TeamDraft, key: string): TeamDraft {
  return {
    ...draft,
    entry_key: draft.entry_key === key ? null : draft.entry_key,
    nodes: draft.nodes.filter((node) => node.key !== key),
    edges: draft.edges.filter((edge) => edge.from_key !== key && edge.to_key !== key),
  }
}

/** 给一张图加连线（只动边，供画布复用）；同向同类边已存在时原样返回。 */
export function addEdgeToGraph(
  nodes: CollabNode[],
  edges: CollabEdge[],
  fromKey: string,
  toKey: string,
  kind?: CollabEdgeKind,
  ports?: { fromSide?: CollabEdgePort | null; toSide?: CollabEdgePort | null },
): CollabEdge[] {
  const resolved = kind ?? inferEdgeKind(
    nodes.find((node) => node.key === fromKey),
    nodes.find((node) => node.key === toKey),
  )
  if (edges.some((edge) => edge.from_key === fromKey && edge.to_key === toKey && edge.kind === resolved)) {
    return edges
  }
  return [
    ...edges,
    {
      id: newEdgeId(), from_key: fromKey, to_key: toKey, kind: resolved, label: '', max_rounds: 0,
      from_side: ports?.fromSide ?? null, to_side: ports?.toSide ?? null,
    },
  ]
}

/** 草稿级加连线：同向同类边已存在时返回原草稿。 */
export function addEdgeToDraft(draft: TeamDraft, fromKey: string, toKey: string, kind?: CollabEdgeKind): TeamDraft {
  const edges = addEdgeToGraph(draft.nodes, draft.edges, fromKey, toKey, kind)
  return edges === draft.edges ? draft : { ...draft, edges }
}
