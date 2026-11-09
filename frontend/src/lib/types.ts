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
}

export interface Availability {
  installed: boolean
  version?: string | null
  detail?: string | null
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
  kind: 'thinking' | 'tool_call' | 'tool_result'
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

export interface TestResult {
  ok: boolean
  message: string
  checks: { name: string; ok: boolean; detail: string }[]
}
