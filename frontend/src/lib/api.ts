import type {
  AgentEvent, AgentInstance, AgentType, Conversation, Message, Overview, Role, TestResult,
} from './types'

const BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body)
    } catch {
      /* body was not json */
    }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  overview: () => request<Overview>('/api/system/overview'),
  agentTypes: () => request<{ items: AgentType[] }>('/api/agent-types').then((r) => r.items),

  listAgents: () => request<{ items: AgentInstance[] }>('/api/agents').then((r) => r.items),
  createAgent: (body: unknown) => request<AgentInstance>('/api/agents', { method: 'POST', body: JSON.stringify(body) }),
  updateAgent: (id: string, body: unknown) =>
    request<AgentInstance>(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAgent: (id: string) => request<void>(`/api/agents/${id}`, { method: 'DELETE' }),
  duplicateAgent: (id: string) => request<AgentInstance>(`/api/agents/${id}/duplicate`, { method: 'POST' }),
  testAgent: (id: string) => request<TestResult>(`/api/agents/${id}/test`, { method: 'POST' }),

  listRoles: () => request<{ items: Role[] }>('/api/roles').then((r) => r.items),
  createRole: (body: unknown) => request<Role>('/api/roles', { method: 'POST', body: JSON.stringify(body) }),
  updateRole: (id: string, body: unknown) => request<Role>(`/api/roles/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteRole: (id: string) => request<void>(`/api/roles/${id}`, { method: 'DELETE' }),
  duplicateRole: (id: string) => request<Role>(`/api/roles/${id}/duplicate`, { method: 'POST' }),

  listConversations: () => request<{ items: Conversation[] }>('/api/conversations').then((r) => r.items),
  createConversation: (body: unknown) =>
    request<Conversation>('/api/conversations', { method: 'POST', body: JSON.stringify(body) }),
  getConversation: (id: string) =>
    request<{ conversation: Conversation; messages: Message[]; active_run: unknown }>(`/api/conversations/${id}`),
  updateConversation: (id: string, body: unknown) =>
    request<Conversation>(`/api/conversations/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteConversation: (id: string) => request<void>(`/api/conversations/${id}`, { method: 'DELETE' }),
  cancelRun: (conversationId: string) =>
    request<{ ok: boolean }>(`/api/conversations/${conversationId}/cancel`, { method: 'POST' }),
  resetSession: (conversationId: string) =>
    request<{ ok: boolean }>(`/api/conversations/${conversationId}/session/reset`, { method: 'POST' }),
}

/**
 * POST + Server-Sent-Events. `EventSource` cannot send a body, so we read the
 * stream manually and hand every parsed event to `onEvent`.
 */
export async function streamPost(
  path: string,
  body: unknown,
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok || !response.body) {
    let detail = response.statusText
    try {
      const parsed = await response.json()
      detail = typeof parsed?.detail === 'string' ? parsed.detail : detail
    } catch {
      /* ignore */
    }
    throw new ApiError(response.status, detail)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let boundary = buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const frame = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const data = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trim())
        .join('\n')
      if (data) {
        try {
          onEvent(JSON.parse(data) as AgentEvent)
        } catch {
          /* heartbeat or partial frame */
        }
      }
      boundary = buffer.indexOf('\n\n')
    }
  }
}
