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
  /** 键值对字段（`keyvalue`）里每个键对应的「值」占位提示（键 → 提示文字）。
   *  密钥类的值不下发到浏览器，只能把掩码当占位符，编辑时才分得清「空着」还是「已填好」。 */
  entryPlaceholders?: Record<string, string | null> | null
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
  /** 这份结论的检测时间（框架缓存的，ISO 字符串；实时探测与从未检测时为 null） */
  checked_at?: string | null
}

/** 运行时维护动作：安装 / 更新 / 卸载。 */
export type RuntimeAction = 'install' | 'update' | 'uninstall'

/** 适配器声明的维护命令（由后端原样执行，前端只负责展示与触发）。 */
export interface RuntimeCommands {
  install: string[]
  update: string[]
  uninstall: string[]
  latest: string[]
}

export interface RuntimeProvisioning {
  /** 该运行时是否由平台管理（进程内 API 内核、自定义 CLI 为 false） */
  supported: boolean
  manager?: string | null
  package?: string | null
  homepage?: string | null
  note?: string | null
  commands?: RuntimeCommands | null
  installed: boolean
  version?: string | null
  /** 已安装时的可执行文件路径 */
  path?: string | null
  /**
   * 这份安装状态的检测时间（ISO 字符串）。
   * 可用性由后端缓存并定时检测（每天定点 + 启动补齐 + 安装后重测），
   * 所以卡片上可以直接写「检测于 …」，而不是每次打开页面去跑一遍 `内核 --version`。
   */
  checked_at?: string | null
}

/** 一个智能体运行时（某个 CLI 内核在本机的那份可执行文件）。 */
export interface AgentRuntime {
  id: string
  name: string
  vendor: string
  description: string
  runtime: 'cli' | 'api'
  docs_url?: string | null
  /** 官方 Logo 的静态资源路径（后端按适配器声明给出，前端只负责显示） */
  logo?: string | null
  executable?: string | null
  /** 有多少个智能体在用这个运行时 */
  agents: number
  provisioning: RuntimeProvisioning
}

/** 「检查更新」的结果。 */
export interface RuntimeLatest {
  supported: boolean
  latest?: string | null
  command?: string | null
  error?: string | null
}

/** 运行时维护动作的 SSE 事件（自有的 provision.* 信封，非适配器事件协议）。 */
export interface ProvisionEvent {
  type: string
  runtime_id?: string
  action?: RuntimeAction
  command?: string | null
  manager?: string | null
  text?: string
  level?: string
  ok?: boolean
  status?: 'ok' | 'error' | 'cancelled'
  exit_code?: number | null
  installed?: boolean
  version?: string | null
  path?: string | null
  error?: string | null
}

/** 后台安装 / 更新 / 卸载任务的摘要（刷新页面后恢复状态用）。 */
export interface ProvisionJobInfo {
  runtime_id: string
  name: string
  action: RuntimeAction
  status: 'running' | 'ok' | 'error'
  error?: string | null
  started_at: number
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
  /** 该内核访问模型时使用的协议（openai / anthropic / gemini），用于匹配自定义网关 */
  protocol?: string | null
  /** 该内核默认的鉴权方式（bearer / x-api-key / query / none） */
  default_auth_style?: string | null
  /** 该内核官方链路的预置模型（网关「必须成功才能保存」时的兜底清单） */
  default_models?: string[]
  docs_url?: string | null
  executable?: string | null
  /** 运行时的安装 / 更新方式（`/api/agent-types` 也带上，与「运行时」页同源） */
  provisioning?: RuntimeProvisioning
  availability: Availability
  system_defaults?: SystemDefaults
}

/** 一个网关：自定义网关或内核的官方链路，是 API 密钥的唯一来源。 */
export interface Gateway {
  id: string
  name: string
  /** official = 内核官方链路（播种生成、不可删）；custom = 用户自建 */
  kind: 'official' | 'custom'
  /** 官方链路对应的内核 id；自定义网关为 null */
  type_id?: string | null
  /** openai / anthropic / gemini */
  protocol: string
  /** bearer / x-api-key / query / none */
  auth_style: string
  base_url: string
  /** 模型白名单（智能体只能从这里面选模型） */
  selected_models: string[]
  /** 最近一次「获取模型」探测到的完整清单 */
  model_catalog: string[]
  extra_headers: Record<string, unknown>
  enabled: boolean
  created_at: string
  updated_at: string
  last_probed_at?: string | null
  type_name?: string | null
  api_key_set: boolean
  api_key_preview?: string | null
  /** 有多少个未删除的智能体在用这个网关 */
  used_by_profiles: number
}

/** 「获取模型」的探测结果（不落库）。 */
export interface GatewayProbeResult {
  ok: boolean
  message: string
  models: string[]
}

/** 下拉选项（协议 / 鉴权方式的候选值）。 */
export interface GatewayOption {
  value: string
  label: string
}

/** `GET /api/gateways` 的返回：网关列表 + 协议 / 鉴权的候选值。 */
export interface GatewayList {
  items: Gateway[]
  protocols: GatewayOption[]
  auth_styles: GatewayOption[]
}

export interface AgentSkillBrief {
  id: string
  name: string
  type_id: string
  source_kind: string
  available: boolean
}

/** 智能体挂着的 MCP 服务器摘要（密钥永不回传）。 */
export interface AgentMcpBrief {
  id: string
  name: string
  slug: string
  transport: string
  /** 来源内核 id；为空表示平台通用 */
  type_id?: string | null
  available: boolean
  enabled: boolean
}

/** 智能体：人格 + 内核 + 网关 + 模型（原「角色」与「智能体实例」合并而来）。 */
/** 凭据库里的凭据：全局一份，智能体按引用使用。值永不回传，只有掩码。 */
export interface Credential {
  id: string
  /** 同时就是要注入的环境变量名 */
  name: string
  description?: string | null
  has_value: boolean
  masked?: string | null
  /** 有多少个智能体在引用它 */
  used_by_agents: number
  created_at: string
  updated_at: string
}

/** 智能体引用到的凭据摘要（卡片 / 弹窗上展示）。 */
export interface AgentCredentialBrief {
  id: string
  name: string
  description?: string | null
  has_value: boolean
  masked?: string | null
}

/**
 * 提交给后端的一条凭据引用。
 * `id` 有值＝挑凭据库里已有的那条；没有 `id` 时按 `name` 找同名凭据，
 * 再找不到就用 `value` 新建一条（于是「直接挑」和「顺手新建」都是一次保存搞定）。
 */
export interface AgentCredentialInput {
  id?: string | null
  name: string
  description?: string | null
  value?: string | null
}

export interface AgentProfile {
  id: string
  name: string
  avatar: string
  tagline?: string | null
  description?: string | null
  system_prompt: string
  type_id: string
  gateway_id?: string | null
  /** 新对话默认使用的模型（具体对话可以在对话里改） */
  model?: string | null
  options: Record<string, unknown>
  enabled: boolean
  /** 默认智能体：新建对话不再先问选谁，直接用这颗星标着的那个（全局唯一） */
  is_default: boolean
  created_at: string
  updated_at: string
  type_name?: string | null
  runtime?: 'cli' | 'api' | null
  availability?: Availability | null
  gateway_name?: string | null
  gateway_base_url?: string | null
  gateway_kind?: string | null
  gateway_api_key_set: boolean
  /** 智能体引用到的凭据（本地加密保存，运行时按环境变量注入；值绝不回传） */
  credentials: AgentCredentialBrief[]
  /** 该智能体分配到的技能（技能只能通过分配获得） */
  skill_ids: string[]
  skills: AgentSkillBrief[]
  /** 该智能体挂着的 MCP 服务器（同样只能通过分配获得） */
  mcp_ids: string[]
  mcp_servers: AgentMcpBrief[]
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
  used_by_agents: number
  /** 由「技能市场」装入的技能为 true（可在技能页卸载），其余只读 */
  is_market: boolean
  /** 市场技能对应的市场页地址 */
  source_url?: string | null
}

export interface SkillDetail extends Skill {
  path: string
  fingerprint: string
  allowed_tools: string[]
  files: { path: string; size: number }[]
  meta: Record<string, unknown>
  body: string
  body_truncated: boolean
  /** 引用了这个技能的智能体 */
  agents: { id: string; name: string; avatar: string }[]
}

export interface SkillRescanResult {
  added: number
  updated: number
  missing: number
  total: number
}

/**
 * 统一编目的一个 MCP 服务器。
 *
 * 两类来源：平台新建的「通用」服务器（`type_id` 为空，可分配给任意内核的智能体），
 * 以及从某个内核自己的配置里只读扫出来的（`type_id` 是那个内核，只能在同内核里分配）。
 */
export interface McpServer {
  id: string
  name: string
  slug: string
  description: string
  type_id?: string | null
  type_name?: string | null
  source_kind: string
  source_label: string
  /** stdio / http / sse */
  transport: string
  command?: string | null
  args: string[]
  url?: string | null
  /** 只给键名与掩码，永远没有明文 */
  env_keys: string[]
  env_preview: Record<string, string | null>
  header_keys: string[]
  header_preview: Record<string, string | null>
  enabled: boolean
  available: boolean
  note?: string | null
  /** 平台是否接管它的内容（平台新建的为 true，从内核配置扫来的为 false） */
  editable: boolean
  used_by_agents: number
}

export interface McpServerDetail extends McpServer {
  /** 挂着这个服务器的智能体 */
  agents: { id: string; name: string; avatar: string }[]
}

export interface McpRescanResult {
  added: number
  updated: number
  missing: number
  total: number
}

/** MCP 服务器的「新建 / 编辑」载荷（与后端 McpServerCreate/Update 字段一致）。 */
export interface McpServerPayload {
  name: string
  slug?: string | null
  description?: string | null
  /** stdio / http / sse */
  transport: string
  command?: string | null
  url?: string | null
  args?: string[]
  /** 值加密保存；编辑时留空＝保持原值，删掉整行＝删除 */
  env?: Record<string, string>
  headers?: Record<string, string>
  enabled?: boolean
}

/** 技能市场（SkillHub）里的一个技能。 */
export interface MarketSkill {
  slug: string
  name: string
  description: string
  version?: string | null
  author?: string | null
  category?: string | null
  downloads: number
  stars: number
  score: number
  owner?: string | null
  source?: string | null
  verified: boolean
  requires_api_key: boolean
  security?: string | null
  icon_url?: string | null
  updated_at?: number | null
  url: string
}

export interface MarketSkillList {
  items: MarketSkill[]
  page: number
  page_size: number
  total: number
  has_more: boolean
}

/** 可安装技能的内核（声明了可写技能根的适配器）。 */
export interface MarketKernel {
  type_id: string
  name: string
  path?: string | null
}

/** 安装市场技能的请求体。 */
export interface MarketInstallInput {
  slug: string
  type_id: string
  version?: string | null
}

/** 安装市场技能的结果。 */
export interface MarketInstallResult {
  skill_id: string
  name: string
  slug: string
  type_id: string
  version?: string | null
  requires_api_key: boolean
  warnings: string[]
}

/** 正在跑的那一次运行（`GET /conversations/{id}` 的 `active_run`）。 */
export interface ActiveRunInfo {
  id: string
  conversation_id: string
  agent_name: string
  status: string
  elapsed_seconds: number
  /** 本轮助手消息 id：界面据此隐藏数据库里那条空占位，改用实时卡片渲染 */
  assistant_message_id?: string | null
  user_message_id?: string | null
}

/** 单条对话的完整读取结果：对话本身 + 全部消息 + 正在跑的那次运行（若有）。 */
export interface ConversationDetail {
  conversation: Conversation
  messages: Message[]
  active_run: ActiveRunInfo | null
}

/** 这条对话可选的模型（`GET /conversations/{id}/models`）。 */
export interface ConversationModelOptions {
  /** 当前对话单独指定的模型；为空表示跟随智能体默认 */
  current?: string | null
  /** 智能体的默认模型 / 内核默认 */
  default?: string | null
  /** 自定义网关名；为空表示官方默认链路 */
  gateway?: string | null
  /** 网关白名单里的模型；官方默认时为空，前端允许直接填写模型名 */
  models: string[]
}

export interface Conversation {
  id: string
  title: string
  agent_profile_id: string
  session_id?: string | null
  workspace_dir?: string | null
  /** 这条对话单独指定的模型；为空表示跟随智能体默认 */
  model?: string | null
  archived: boolean
  created_at: string
  updated_at: string
  agent_name?: string | null
  agent_avatar?: string | null
  agent_type_id?: string | null
  message_count: number
  /** 最后一条是已完成的 AI 回复、且晚于用户最后一次打开它的时间（列表接口算出来） */
  unread?: boolean
}

/** `GET /conversations` 的返回：对话列表 + 当前正在跑的那些运行。 */
export interface ConversationListSnapshot {
  items: Conversation[]
  /** 有运行在进行中的对话：列表据此显示「正在回复」的动画 */
  active_runs: ActiveRunInfo[]
}

/**
 * 上传到对话工作目录的附件（`POST /api/conversations/{id}/attachments` 的返回）。
 * 发送消息时把 `rel_path` 放进 `attachments`，框架会连同路径一起写进提示词。
 */
export interface AttachmentRef {
  /** 落盘后的文件名（已去掉目录成分） */
  name: string
  /** 相对工作目录的路径，发送消息时用这个值 */
  rel_path: string
  /** 绝对路径（只在界面上展示，供用户复制） */
  path: string
  size: number
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

/* ---------- 用量页（后端 app/api/usage.py）---------- */

/** 一组归一化后的 token 计数（不同适配器的字段名在后端已统一）。 */
export interface UsageTokens {
  input: number
  output: number
  total: number
  cache_read: number
  cache_creation: number
  /** 缓存命中率（0~1）；没有可用的分母时为 null */
  cache_ratio: number | null
  /** 参与统计的消息条数 */
  messages: number
}

export interface UsageAgentRow {
  id: string
  name: string
  avatar: string
  type_id?: string | null
  type_name?: string | null
  tokens: UsageTokens
}

export interface UsageConversationRow {
  id: string
  title: string
  agent_name?: string | null
  agent_avatar?: string | null
  updated_at?: string | null
  tokens: UsageTokens
}

export interface UsageDayRow {
  date: string
  input: number
  output: number
  total: number
}

export interface UsageSkillRow {
  id: string
  name: string
  type_id: string
  type_name?: string | null
  source_label: string
  /** 所选时间范围内这个技能被调用的次数 */
  calls: number
}

export interface UsageSkills {
  /** 所选范围内的技能调用总次数 */
  calls: number
  /** 所选范围内被调用过的技能数 */
  skills_used: number
  /** 被调用最多的技能（只含所选范围内调用过至少一次的） */
  top: UsageSkillRow[]
}

export interface UsageOverview {
  tokens: UsageTokens
  /** 产生过 token 消耗的对话数 */
  conversations: number
  by_agent: UsageAgentRow[]
  by_conversation: UsageConversationRow[]
  daily: UsageDayRow[]
  skills: UsageSkills
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
   协作域（多智能体工单协作）—— 与后端 app/collab/schemas.py 一一对应
   ============================================================ */

/** 节点在协作里的岗位 */
export type CollabDuty = 'coordinator' | 'engineer' | 'reviewer'
/** 有向边的类型：派发 / 审查 / 打回 */
export type CollabEdgeKind = 'assign' | 'review' | 'rework'
/** 节点端口：输入在左/上，输出在右/下。 */
export type CollabEdgePort = 'left' | 'right' | 'top' | 'bottom'
export type CollabWorkspaceScope = 'none' | 'read' | 'write'
/** 一个节点在一轮里能发起的动作 */
export type CollabTicketAction =
  | 'delegate' | 'submit' | 'review' | 'done' | 'ask_user' | 'escalate' | 'reject'

/** `ask_user` 的一条可点选项；`freeform` 表示点它只是把焦点交给输入框。 */
export interface CollabChoice {
  label: string
  value: string
  description?: string | null
  freeform?: boolean
}

export interface CollabNode {
  key: string
  agent_profile_id: string
  label: string
  duty: CollabDuty
  instructions: string
  capability_scope: string
  /** null 表示按 duty 取默认值 */
  workspace_scope: CollabWorkspaceScope | null
  /** 空数组表示按 duty 取默认动作集合 */
  output_contract: CollabTicketAction[]
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
  /** 画布端口方向；旧数据为空时由画布按节点相对位置自动推断。 */
  from_side?: CollabEdgePort | null
  to_side?: CollabEdgePort | null
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
  /** 前端归一化后写入的严重级别；服务端已分别放在 errors / warnings 两个数组里 */
  severity?: 'error' | 'warning'
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

/** `PUT /api/collab/teams/{id}` 的请求体（对应后端 `TeamUpdate`：整图替换）。 */
export interface CollabTeamUpdate {
  name?: string
  description?: string | null
  entry_key?: string | null
  workspace_dir?: string | null
  graph?: CollabGraph
  budget?: CollabBudget
}

/** `POST /api/collab/teams/{id}/validate` 的请求体（对应后端 `TeamValidateIn`）。 */
export interface CollabTeamValidateIn {
  graph?: CollabGraph
  entry_key?: string | null
}

export type CollabRunStatus = 'running' | 'waiting_user' | 'done' | 'error' | 'cancelled' | 'interrupted'

export type CollabTicketState =
  | 'open' | 'working' | 'waiting_children' | 'review' | 'rework'
  | 'escalated' | 'done' | 'rejected' | 'blocked'

/** 一次上报：`from` 谁上报的、`reason` 卡在哪、`need` 需要上层做什么、`hops` 已经过谁 */
export interface CollabEscalation {
  from?: string
  reason?: string
  need?: string
  hops?: string[]
}

export interface CollabTicket {
  id: string
  run_id: string
  /** 非空表示这是拆解出来的子工单 */
  parent_ticket_id?: string | null
  from_agent_profile_id?: string | null
  to_agent_profile_id: string
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
  /** 最近一次上报记录；非空表示这张工单曾被上报过 */
  escalation?: CollabEscalation
  created_at: string
  updated_at: string
  closed_at?: string | null
}

export interface CollabStep {
  id: string
  run_id: string
  ticket_id?: string | null
  /** 用户自己的发言没有智能体 */
  agent_profile_id?: string | null
  profile_name?: string | null
  /** 节点 key；用户自己的发言是 "user" */
  role_key: string
  duty: string
  sequence: number
  action: string
  /** delegate 把活交给了哪个节点 key；空串表示不是派发（用于还原交接链） */
  target_key?: string
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

/** 入口智能体聊天的一条消息（对应后端 `collab_messages`）。 */
export interface CollabChatMessage {
  id: string
  run_id: string
  /** user = 你；leader = 入口智能体 */
  sender: 'user' | 'leader'
  role_key: string
  agent_profile_id?: string | null
  sequence: number
  status: string
  content: string
  parts: MessagePart[]
  usage: Record<string, number>
  error?: string | null
  /** 需求细化与同步结果 */
  meta: Record<string, unknown>
  created_at: string
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
  chat_messages: CollabChatMessage[]
  /** 刷新时是否有一条 CEO 回复仍在生成 */
  chat_active: boolean
}

/* ---------- 协作信封事件（app/collab/protocol.py）---------- */

export type CollabEventType =
  | 'collab.run.started' | 'collab.step.started' | 'collab.step.event' | 'collab.step.finished'
  | 'collab.ticket.created' | 'collab.ticket.updated' | 'collab.run.status' | 'collab.notice'
  | 'collab.chat.started' | 'collab.chat.event' | 'collab.chat.finished'

/** 传输层在 SSE 收尾时补发的事件（不属于协作信封）：用来区分「正常结束」与「连接被切断」。 */
export interface StreamClosedEvent {
  type: 'stream.closed'
}

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
  role_key?: string
  duty?: string
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
  /** `collab.notice` 且 level=ask 时一并带上，便于即时渲染决策卡 */
  question?: string
  options?: CollabChoice[]
  /** 聊天事件：这一条回复的消息 ID */
  message_id?: string
  /** 聊天结束时的解析/同步告警 */
  warning?: string | null
  /** 聊天里识别到并同步进工单的需求更新 */
  applied?: {
    ticket_ids?: string[]
    ticket_count?: number
    fields?: string[]
    update?: Record<string, unknown>
  }
}
