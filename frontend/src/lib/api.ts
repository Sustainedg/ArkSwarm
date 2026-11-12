import type {
  AgentEvent, AgentInstance, AgentRuntime, AgentType, ArchitectureDoc, AttachmentRef, CollabChatMessage, CollabRun,
  CollabRunDetail, CollabTeam, CollabTeamUpdate, CollabTeamValidateIn, CollabValidation, Conversation,
  Message, Overview, ProvisionEvent, Role, RuntimeLatest, Skill, SkillDetail, SkillRescanResult, TestResult,
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
  const body = await readBody(response)
  if (!response.ok) {
    throw new ApiError(response.status, describeBody(response, body))
  }
  if (response.status === 204 || !body.text.trim()) return undefined as T
  if (!body.isJson) throw new ApiError(response.status, describeBody(response, body))
  return body.data as T
}

interface ResponseBody {
  /** 原始响应文本（空响应体就是空串） */
  text: string
  /** 解析后的 JSON；不是 JSON 时忽略 */
  data: unknown
  isJson: boolean
}

/**
 * 先把响应体读成文本，再自己 `JSON.parse`。
 *
 * 不直接调 `response.json()`：它对「空响应体 / 非 JSON」在 WebKit 上抛的是
 * "The string did not match the expected pattern."，Chromium 上是另一种说法 ——
 * 一句与业务无关的浏览器方言，用户与开发者都无从下手（见 main.py 的 `/api/` 404 说明）。
 */
async function readBody(response: Response): Promise<ResponseBody> {
  const text = await response.text()
  if (!text.trim()) return { text, data: undefined, isJson: false }
  try {
    return { text, data: JSON.parse(text), isJson: true }
  } catch {
    return { text, data: undefined, isJson: false }
  }
}

/** 把一次响应翻译成能直接展示的中文：接口自己报的 `detail` 优先，其次说清楚「拿到的不是 JSON」。 */
function describeBody(response: Response, body: ResponseBody): string {
  if (body.isJson) {
    const detail = (body.data as { detail?: unknown } | null)?.detail
    if (typeof detail === 'string') return detail
    return JSON.stringify(detail ?? body.data)
  }
  const contentType = response.headers.get('content-type')?.split(';')[0]?.trim() || '未知类型'
  const reason = body.text.trim() ? '返回的是 HTML 等非 JSON 内容' : '响应体是空的'
  return (
    `接口没有返回 JSON（HTTP ${response.status}，Content-Type: ${contentType}，${reason}）。` +
    '通常是接口不存在，或反向代理 / 静态兜底没有把 /api 转发到后端。'
  )
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

  // --- 运行时（各 CLI 内核的安装 / 更新 / 卸载）-----------------------------
  listRuntimes: () => request<{ items: AgentRuntime[] }>('/api/runtimes').then((r) => r.items),
  checkRuntimeLatest: (typeId: string) =>
    request<RuntimeLatest>(`/api/runtimes/${typeId}/check`, { method: 'POST' }),
  /** 安装 / 更新不改动已有配置，直接发起；卸载是破坏性操作，必须带 confirm（服务端会再校验一次）。 */
  installRuntime: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamPost<ProvisionEvent>(`/api/runtimes/${typeId}/install`, {}, onEvent, signal),
  updateRuntime: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamPost<ProvisionEvent>(`/api/runtimes/${typeId}/update`, {}, onEvent, signal),
  uninstallRuntime: (typeId: string, onEvent: (event: ProvisionEvent) => void, signal?: AbortSignal) =>
    streamPost<ProvisionEvent>(`/api/runtimes/${typeId}/uninstall`, { confirm: true }, onEvent, signal),

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
  /** 上传附件：内容走 base64，省掉 multipart 依赖；服务端落到对话工作目录的 attachments/ */
  uploadAttachment: (conversationId: string, body: { name: string; content_base64: string }) =>
    request<AttachmentRef>(`/api/conversations/${conversationId}/attachments`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // --- 协作域 ---------------------------------------------------------------
  // 协作组（可视化编排图）
  listCollabTeams: () => request<{ items: CollabTeam[] }>('/api/collab/teams').then((r) => r.items),
  getCollabTeam: (id: string) => request<CollabTeam>(`/api/collab/teams/${id}`),
  createCollabTeam: (body: unknown) =>
    request<CollabTeam>('/api/collab/teams', { method: 'POST', body: JSON.stringify(body) }),
  saveCollabTeam: (id: string, body: CollabTeamUpdate) =>
    request<CollabTeam>(`/api/collab/teams/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  /** 只校验不保存：编辑器每改一笔就调一次 */
  validateCollabTeam: (id: string, body: CollabTeamValidateIn) =>
    request<CollabValidation>(`/api/collab/teams/${id}/validate`, { method: 'POST', body: JSON.stringify(body) }),
  duplicateCollabTeam: (id: string) =>
    request<CollabTeam>(`/api/collab/teams/${id}/duplicate`, { method: 'POST' }),
  deleteCollabTeam: (id: string) => request<void>(`/api/collab/teams/${id}`, { method: 'DELETE' }),

  // 协作运行
  listCollabRuns: (teamId?: string) => {
    const suffix = teamId ? `?team_id=${encodeURIComponent(teamId)}` : ''
    return request<{ items: CollabRun[] }>(`/api/collab/runs${suffix}`).then((r) => r.items)
  },
  createCollabRun: (body: { team_id: string; title?: string; workspace_dir?: string | null }) =>
    request<CollabRun>('/api/collab/runs', { method: 'POST', body: JSON.stringify(body) }),
  getCollabRun: (id: string) => request<CollabRunDetail>(`/api/collab/runs/${id}`),
  /** 上传协作附件：落到这次协作的工作目录，发消息时随聊天通道带上（同对话附件） */
  uploadCollabAttachment: (runId: string, body: { name: string; content_base64: string }) =>
    request<AttachmentRef>(`/api/collab/runs/${runId}/attachments`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  listCollabChat: (id: string) =>
    request<{ items: CollabChatMessage[] }>(`/api/collab/runs/${id}/chat`).then((r) => r.items),
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
/** 消费一条 SSE 事件流（POST 推进 / GET 订阅共用这一份读取逻辑）。 */
async function streamRequest<T>(
  path: string,
  init: RequestInit,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Accept: 'text/event-stream', ...(init.headers ?? {}) },
    signal,
  })
  if (!response.ok || !response.body) {
    throw new ApiError(response.status, describeBody(response, await readBody(response)))
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

export function streamPost<T = AgentEvent>(
  path: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamRequest<T>(
    path,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
    onEvent,
    signal,
  )
}

/** 订阅一条 GET SSE（用于断开后重连协作运行、或在另一台设备上实时围观）。 */
export function streamGet<T = AgentEvent>(
  path: string,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamRequest<T>(path, { method: 'GET' }, onEvent, signal)
}
