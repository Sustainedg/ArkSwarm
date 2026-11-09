export type FieldType =
  | 'string' | 'secret' | 'number' | 'boolean' | 'select' | 'textarea' | 'keyvalue' | 'string_list'

export interface ConfigField {
  key: string
  label: string
  type: FieldType
  required: boolean
  default: unknown
  placeholder?: string | null
  help?: string | null
  options?: { value: string; label: string }[] | null
  advanced: boolean
}

export interface Capabilities {
  streaming: boolean
  sessions: boolean
  native_system_prompt: boolean
  tools: boolean
  mcp: boolean
  permission_modes: boolean
  working_directory: boolean
  /** 该内核是否支持技能（技能来自各内核自己的技能目录，由平台统一编目） */
  skills: boolean
}

export interface Availability {
  installed: boolean
  version?: string | null
  detail?: string | null
}

export interface SystemDefaults {
  gateway_url?: string | null
  model?: string | null
  api_key_set: boolean
  api_key_preview?: string | null
  options?: Record<string, unknown>
}

export interface AgentType {
  id: string
  name: string
  description: string
  vendor: string
  runtime: 'cli' | 'api'
  capabilities: Capabilities
  config_fields: ConfigField[]
  api_key_env?: string | null
  base_url_env?: string | null
  default_gateway?: string | null
  default_model?: string | null
  docs_url?: string | null
  executable?: string | null
  availability: Availability
  system_defaults?: SystemDefaults
}

export interface AgentInstance {
  id: string
  name: string
  type_id: string
  description?: string | null
  gateway_url?: string | null
  model?: string | null
  options: Record<string, unknown>
  enabled: boolean
  created_at: string
  updated_at: string
  type_name?: string | null
  runtime?: 'cli' | 'api' | null
  api_key_set: boolean
  api_key_preview?: string | null
  availability?: Availability | null
}

export interface RoleSkillBrief {
  id: string
  name: string
  type_id: string
  source_kind: string
  available: boolean
}

export interface Role {
  id: string
  name: string
  avatar: string
  tagline?: string | null
  system_prompt: string
  agent_id: string
  workspace_dir?: string | null
  parameters: Record<string, unknown>
  created_at: string
  updated_at: string
  agent_name?: string | null
  agent_type_id?: string | null
  /** 该角色分配到的技能（技能只能通过分配获得） */
  skill_ids: string[]
  skills: RoleSkillBrief[]
}

export interface Skill {
  id: string
  type_id: string
  type_name?: string | null
  slug: string
  name: string
  description: string
  source_kind: string
  source_label: string
  version?: string | null
  author?: string | null
  when_to_use?: string | null
  available: boolean
  total_bytes: number
  file_count: number
  note?: string | null
  used_by_roles: number
}

export interface SkillDetail extends Skill {
  path: string
  fingerprint: string
  allowed_tools: string[]
  files: { path: string; size: number }[]
  meta: Record<string, unknown>
  body: string
  body_truncated: boolean
  roles: { id: string; name: string; avatar: string }[]
}

export interface SkillRescanResult {
  added: number
  updated: number
  missing: number
  total: number
}

export interface Conversation {
  id: string
  title: string
  role_id: string
  agent_id: string
  session_id?: string | null
  workspace_dir?: string | null
  archived: boolean
  created_at: string
  updated_at: string
  role_name?: string | null
  role_avatar?: string | null
  agent_name?: string | null
  message_count: number
}

export interface MessagePart {
  /** text 为正文片段；与 thinking / 工具事件一起按真实发生顺序排列。 */
  kind: 'thinking' | 'text' | 'tool_call' | 'tool_result'
  text?: string
  name?: string
  id?: string | null
  input?: Record<string, unknown>
  output?: string
  is_error?: boolean
}

export interface Message {
  id: string
  conversation_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  parts: MessagePart[]
  usage: Record<string, number>
  status: string
  error?: string | null
  created_at: string
}

export type AgentEventType =
  | 'turn.started' | 'run.started' | 'message.delta' | 'thinking.delta' | 'tool.call'
  | 'tool.result' | 'usage' | 'session' | 'log' | 'error' | 'run.finished' | 'stream.closed'

export interface AgentEvent {
  type: AgentEventType
  text?: string
  name?: string
  call_id?: string
  payload?: Record<string, unknown>
  run_id?: string
  conversation_id?: string
  assistant_message_id?: string
  agent?: string
}

export interface Overview {
  version: string
  version_history: VersionRecord[]
  app_name: string
  data_dir: string
  workspaces_root: string
  agent_types: number
  installed_types: number
  active_runs: { id: string; conversation_id: string; agent_name: string; status: string; elapsed_seconds: number }[]
}

export interface VersionRecord {
  version: string
  date: string
  title: string
  highlights: string[]
}

export interface ArchitectureDoc {
  /** 后端由 docs/architecture.md 实时渲染出的完整 HTML 页面 */
  html: string
  source: string
  updated_at: string
}

export interface TestResult {
  ok: boolean
  message: string
  checks: { name: string; ok: boolean; detail: string }[]
}

/* ============================================================
   协作域（多角色工单协作）—— 与后端 app/collab/schemas.py 一一对应
   ============================================================ */

/** 节点在协作里的岗位 */
export type CollabDuty = 'coordinator' | 'engineer' | 'reviewer'
/** 有向边的类型：派发 / 审查 / 打回 */
export type CollabEdgeKind = 'assign' | 'review' | 'rework'
export type CollabWorkspaceScope = 'none' | 'read' | 'write'
/** 一个节点在一轮里能发起的动作 */
export type CollabTicketAction = 'delegate' | 'submit' | 'review' | 'done' | 'ask_user' | 'reject'

export interface CollabNode {
  key: string
  role_id: string
  label: string
  duty: CollabDuty
  instructions: string
  capability_scope: string
  /** null 表示按 duty 取默认值 */
  workspace_scope: CollabWorkspaceScope | null
  /** 空数组表示按 duty 取默认动作集合 */
  output_contract: CollabTicketAction[]
  /** 节点级适配器参数覆盖（键来自该智能体类型的 config_fields） */
  options_override: Record<string, unknown>
  max_rounds: number
  x: number
  y: number
}

export interface CollabEdge {
  id: string
  from_key: string
  to_key: string
  kind: CollabEdgeKind
  label: string
  /** 这条边额外的返工轮次上限（0 = 不额外限制）：实际上限取「边 / 节点 / 运行时预算」三者最小 */
  max_rounds: number
}

export interface CollabGraph {
  nodes: CollabNode[]
  edges: CollabEdge[]
}

export interface CollabBudget {
  max_steps: number
  max_tickets: number
  max_rounds_per_ticket: number
  max_tokens: number
  timeout_seconds: number
}

export interface CollabIssue {
  code: string
  message: string
  node_key?: string | null
  edge_id?: string | null
}

export interface CollabValidation {
  ok: boolean
  errors: CollabIssue[]
  warnings: CollabIssue[]
}

export interface CollabTeam {
  id: string
  name: string
  description?: string | null
  /** 唯一能与用户对话的节点 key */
  entry_key?: string | null
  workspace_dir?: string | null
  graph: CollabGraph
  budget: CollabBudget
  node_count: number
  created_at: string
  updated_at: string
  validation: CollabValidation
}

export type CollabRunStatus = 'running' | 'waiting_user' | 'done' | 'error' | 'cancelled' | 'interrupted'

export type CollabTicketState =
  | 'open' | 'working' | 'waiting_children' | 'review' | 'rework' | 'done' | 'rejected' | 'blocked'

export interface CollabTicket {
  id: string
  run_id: string
  /** 非空表示这是拆解出来的子工单 */
  parent_ticket_id?: string | null
  from_role_id?: string | null
  to_role_id: string
  /** 谁拆的谁汇总（子工单完成后回到这里） */
  owner_key: string
  /** 现在谁握着 */
  holder_key: string
  /** 最近一次提交待审的节点（打回时默认退回给它） */
  submitter_key: string
  title: string
  brief: { goal?: string; requirements?: string[]; acceptance?: string[]; constraints?: string[]; deliverables?: string[] }
  state: CollabTicketState
  round: number
  max_rounds: number
  verdict?: string | null
  review_comment?: string | null
  result: Record<string, unknown>
  created_at: string
  updated_at: string
  closed_at?: string | null
}

export interface CollabStep {
  id: string
  run_id: string
  ticket_id?: string | null
  role_id?: string | null
  agent_id?: string | null
  role_name?: string | null
  /** 节点 key；用户自己的发言是 "user" */
  role_key: string
  duty: string
  sequence: number
  action: string
  status: string
  content: string
  /** 引擎派生的结构化摘要（下游看到的是它，而不是 content） */
  gist: string
  parts: MessagePart[]
  usage: Record<string, number>
  error?: string | null
  audit: Record<string, unknown>
  started_at: string
  finished_at?: string | null
}

export interface CollabRun {
  id: string
  team_id: string
  team_name?: string | null
  title: string
  goal: string
  status: CollabRunStatus
  entry_key?: string | null
  workspace_dir?: string | null
  /** 本次运行的预算（界面显示「已用 / 上限」） */
  budget: CollabBudget
  steps_used: number
  tickets_used: number
  usage: Record<string, number>
  error?: string | null
  created_at: string
  updated_at: string
  ticket_count: number
  open_tickets: number
}

export interface CollabRunDetail extends CollabRun {
  tickets: CollabTicket[]
  steps: CollabStep[]
}

/* ---------- 协作信封事件（app/collab/protocol.py）---------- */

export type CollabEventType =
  | 'collab.run.started' | 'collab.step.started' | 'collab.step.event' | 'collab.step.finished'
  | 'collab.ticket.created' | 'collab.ticket.updated' | 'collab.run.status' | 'collab.notice'

export interface CollabEvent {
  type: CollabEventType
  run_id?: string
  team_id?: string
  title?: string
  entry_key?: string | null
  step_id?: string
  ticket_id?: string | null
  /** 岗位 key（对应图上的节点） */
  node_key?: string
  duty?: string
  role_name?: string
  agent_name?: string
  /** collab.step.event 里内嵌的原始智能体事件 */
  event?: AgentEvent
  status?: string
  reason?: string | null
  action?: string
  usage?: Record<string, number>
  error?: string | null
  ticket?: CollabTicket
  text?: string
  /** notice 级别：info / warn / error / ask */
  level?: string
}
