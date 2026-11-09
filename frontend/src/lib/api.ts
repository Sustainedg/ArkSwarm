import type {
  AgentEvent, AgentInstance, AgentType, ArchitectureDoc, CollabBudget, CollabGraph, CollabRun,
  CollabRunDetail, CollabTeam, CollabValidation, Conversation, Message, Overview, Role,
  Skill, SkillDetail, SkillRescanResult, TestResult,
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
      /* 响应体不是 JSON */
    }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  overview: () => request<Overview>('/api/system/overview'),
  architecture: () => request<ArchitectureDoc>('/api/system/architecture'),
  agentTypes: () => request<{ items: AgentType[] }>('/api/agent-types').then((r) => r.items),

  listAgents: () => request<{ items: AgentInstance[] }>('/api/agents').then((r) => r.items),
  createAgent: (body: unknown) => request<AgentInstance>('/api/agents', { method: 'POST', body: JSON.stringify(body) }),
  updateAgent: (id: string, body: unknown) =>
    request<AgentInstance>(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAgent: (id: string) => request<void>(`/api/agents/${id}`, { method: 'DELETE' }),
  duplicateAgent: (id: string) => request<AgentInstance>(`/api/agents/${id}/duplicate`, { method: 'POST' }),
  testAgent: (id: string) => request<TestResult>(`/api/agents/${id}/test`, { method: 'POST' }),

  listSkills: (params?: {
    type_id?: string
    source_kind?: string
    q?: string
    available?: boolean
  }) => {
    const search = new URLSearchParams()
    if (params?.type_id) search.set('type_id', params.type_id)
    if (params?.source_kind) search.set('source_kind', params.source_kind)
    if (params?.q) search.set('q', params.q)
    if (params?.available !== undefined) search.set('available', String(params.available))
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request<{ items: Skill[] }>(`/api/skills${suffix}`).then((r) => r.items)
  },
  getSkill: (id: string) => request<SkillDetail>(`/api/skills/${encodeURIComponent(id)}`),
  rescanSkills: () => request<SkillRescanResult>('/api/skills/rescan', { method: 'POST' }),
  setRoleSkills: (roleId: string, skillIds: string[]) =>
    request<Role>(`/api/roles/${roleId}/skills`, {
      method: 'PUT',
      body: JSON.stringify({ skill_ids: skillIds }),
    }),

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

  // --- 协作域 ---------------------------------------------------------------
  // 协作组（可视化编排图）
  listCollabTeams: () => request<{ items: CollabTeam[] }>('/api/collab/teams').then((r) => r.items),
  getCollabTeam: (id: string) => request<CollabTeam>(`/api/collab/teams/${id}`),
  createCollabTeam: (body: unknown) =>
    request<CollabTeam>('/api/collab/teams', { method: 'POST', body: JSON.stringify(body) }),
  saveCollabTeam: (
    id: string,
    body: { name?: string; description?: string | null; entry_key?: string | null; workspace_dir?: string | null; graph?: CollabGraph; budget?: CollabBudget },
  ) => request<CollabTeam>(`/api/collab/teams/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  /** 只校验不保存：编辑器每改一笔就调一次 */
  validateCollabTeam: (id: string, body: { graph?: CollabGraph; entry_key?: string | null }) =>
    request<CollabValidation>(`/api/collab/teams/${id}/validate`, { method: 'POST', body: JSON.stringify(body) }),
  duplicateCollabTeam: (id: string) =>
    request<CollabTeam>(`/api/collab/teams/${id}/duplicate`, { method: 'POST' }),
  deleteCollabTeam: (id: string) => request<void>(`/api/collab/teams/${id}`, { method: 'DELETE' }),

  // 协作运行
  listCollabRuns: (teamId?: string) => {
    const suffix = teamId ? `?team_id=${encodeURIComponent(teamId)}` : ''
    return request<{ items: CollabRun[] }>(`/api/collab/runs${suffix}`).then((r) => r.items)
  },
  createCollabRun: (body: { team_id: string; title?: string }) =>
    request<CollabRun>('/api/collab/runs', { method: 'POST', body: JSON.stringify(body) }),
  getCollabRun: (id: string) => request<CollabRunDetail>(`/api/collab/runs/${id}`),
  deleteCollabRun: (id: string) => request<void>(`/api/collab/runs/${id}`, { method: 'DELETE' }),
  cancelCollabRun: (id: string) =>
    request<{ ok: boolean; cancelled: boolean }>(`/api/collab/runs/${id}/cancel`, { method: 'POST' }),
  /** 把某一步所在的工单放回队列（解析失败/中断后修复，随后续跑） */
  retryCollabStep: (stepId: string) => request<CollabRun>(`/api/collab/steps/${stepId}/retry`, { method: 'POST' }),
}

/**
 * 用 POST 发起 Server-Sent Events 请求。
 * 原生 `EventSource` 无法携带请求体，因此这里手动读取流，
 * 把每个解析出的事件交给 `onEvent`。
 */
export async function streamPost<T = AgentEvent>(
  path: string,
  body: unknown,
  onEvent: (event: T) => void,
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
      /* 忽略 */
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
          onEvent(JSON.parse(data) as T)
        } catch {
          /* 心跳或半包数据 */
        }
      }
      boundary = buffer.indexOf('\n\n')
    }
  }
}
