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
  app_name: string
  data_dir: string
  workspaces_root: string
  agent_types: number
  installed_types: number
  active_runs: { id: string; conversation_id: string; agent_name: string; status: string; elapsed_seconds: number }[]
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
