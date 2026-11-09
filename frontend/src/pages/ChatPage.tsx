import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../components/MessageView'
import { Modal } from '../components/Modal'
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from '../lib/agentStream'
import { api, ApiError, streamPost } from '../lib/api'
import type { Conversation, Message, Role } from '../lib/types'
import { summarizeUsage } from '../lib/usage'

interface LiveTurn extends AgentStreamState {
  userText: string
  roleName: string
  avatar: string
}

export function ChatPage() {
  const { conversationId } = useParams()
  const navigate = useNavigate()

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [messages, setMessages] = useState<Message[]>([])
  const [active, setActive] = useState<Conversation | null>(null)
  const [live, setLive] = useState<LiveTurn | null>(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [busy, setBusy] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  const loadConversations = useCallback(async () => {
    try {
      const [list, roleList] = await Promise.all([api.listConversations(), api.listRoles()])
      setConversations(list)
      setRoles(roleList)
      return list
    } catch (e) {
      setError(describe(e))
      return []
    }
  }, [])

  const openConversation = useCallback(
    async (id: string) => {
      try {
        const detail = await api.getConversation(id)
        setMessages(detail.messages)
        setActive(detail.conversation)
        setLive(null)
      } catch (e) {
        setError(describe(e))
      }
    },
    [],
  )

  useEffect(() => {
    void (async () => {
      const list = await loadConversations()
      if (conversationId) {
        await openConversation(conversationId)
      } else if (list.length > 0) {
        navigate(`/chat/${list[0].id}`, { replace: true })
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [messages, live?.text, live?.parts.length])

  const send = async (content: string, regenerate = false) => {
    const conversation = active
    if (!conversation) return
    if (!regenerate && !content.trim()) return

    setError(null)
    setBusy(true)
    setLive({
      ...emptyAgentStream(),
      userText: regenerate ? '' : content,
      roleName: conversation.role_name ?? '智能体',
      avatar: conversation.role_avatar ?? '🤖',
    })
    if (!regenerate) setDraft('')

    const controller = new AbortController()
    abortRef.current = controller

    try {
      await streamPost(
        regenerate
          ? `/api/conversations/${conversation.id}/regenerate`
          : `/api/conversations/${conversation.id}/messages`,
        regenerate ? {} : { content },
        (event) => setLive((prev) => (prev ? { ...prev, ...applyAgentEvent(prev, event) } : prev)),
        controller.signal,
      )
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') setError(describe(e))
    } finally {
      abortRef.current = null
      setBusy(false)
      try {
        const detail = await api.getConversation(conversation.id)
        setMessages(detail.messages)
        setActive(detail.conversation)
        setLive(null)
        await loadConversations()
      } catch {
        /* 刷新失败时保留流式渲染出来的内容 */
      }
    }
  }

  const stop = async () => {
    if (!active) return
    abortRef.current?.abort()
    await api.cancelRun(active.id).catch(() => undefined)
  }

  const createConversation = async (roleId: string) => {
    try {
      const conversation = await api.createConversation({ role_id: roleId })
      setPickerOpen(false)
      await loadConversations()
      navigate(`/chat/${conversation.id}`)
    } catch (e) {
      setError(describe(e))
    }
  }

  const chatMessages: RenderMessage[] = messages.map((message) => ({
    id: message.id,
    role: message.role,
    content: message.content,
    parts: message.parts,
    status: message.status,
    error: message.error,
    usage: message.usage,
  }))

  const liveMessage: RenderMessage | null = live
    ? {
        id: 'live',
        role: 'assistant',
        content: live.text,
        parts: live.parts,
        status: live.error ? 'error' : 'streaming',
        error: live.error,
        usage: live.usage,
        streaming: live.status === 'running',
        logs: live.logs,
      }
    : null

  // 整段对话（含正在流式输出的这一轮）累计的 token 消耗。
  const usageSummary = useMemo(
    () =>
      summarizeUsage([
        ...messages.filter((m) => m.role === 'assistant').map((m) => m.usage),
        live?.usage,
      ]),
    [messages, live?.usage],
  )

  return (
    <div className="chat-shell">
      <aside className="chat-list">
        <button className="btn primary" style={{ justifyContent: 'center' }} onClick={() => setPickerOpen(true)}>
          + 新建对话
        </button>
        {conversations.map((conversation) => {
          const isActive = conversation.id === active?.id || conversation.id === conversationId
          return (
            <div
              key={conversation.id}
              className={`chat-item${isActive ? ' active' : ''}`}
              onClick={() => navigate(`/chat/${conversation.id}`)}
            >
              <div className="chat-item-title">
                {conversation.role_avatar} {conversation.title}
              </div>
              <div className="chat-item-meta">
                <span>{conversation.role_name}</span>
                <span>·</span>
                <span>{conversation.message_count} 条</span>
              </div>
            </div>
          )
        })}
        {conversations.length === 0 && (
          <p className="dim small" style={{ padding: '14px 8px', lineHeight: 1.6 }}>
            还没有对话。先在「角色」页创建一个角色，再回到这里开始聊天。
          </p>
        )}
      </aside>

      <section className="chat-pane">
        {active ? (
          <>
            <div className="chat-topbar">
              <div className="avatar">{active.role_avatar}</div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600 }}>{active.role_name}</div>
                <div className="dim small">
                  {active.agent_name} · {active.session_id ? `会话 ${active.session_id.slice(0, 8)}` : '新会话'}
                </div>
              </div>
              <div className="spacer" style={{ flex: 1 }} />
              <div className="row" style={{ gap: 6 }}>
                <button
                  className="btn sm"
                  onClick={() => void send('', true)}
                  disabled={busy || messages.length === 0}
                  title="用当前角色配置重新生成最后一条回复"
                >
                  ↻ 重新生成
                </button>
                <button
                  className="btn sm"
                  onClick={async () => {
                    await api.resetSession(active.id)
                    await openConversation(active.id)
                  }}
                  disabled={busy || !active.session_id}
                  title="丢弃智能体侧的会话记忆，下一轮从零开始"
                >
                  ⟲ 重置记忆
                </button>
                <button
                  className="btn sm danger"
                  onClick={async () => {
                    if (!confirm(`删除对话「${active.title}」？`)) return
                    await api.deleteConversation(active.id)
                    const list = await loadConversations()
                    if (list.length > 0) navigate(`/chat/${list[0].id}`)
                    else {
                      setActive(null)
                      setMessages([])
                      navigate('/chat')
                    }
                  }}
                >
                  删除
                </button>
              </div>
            </div>

            <div className="chat-scroll" ref={scrollRef}>
              <div className="chat-thread">
                {chatMessages.length === 0 && !liveMessage && (
                  <div className="empty">
                    <div className="empty-icon">{active.role_avatar}</div>
                    <p>与「{active.role_name}」的对话已就绪</p>
                    <p className="dim small" style={{ marginTop: 6 }}>
                      智能体：{active.agent_name} · 工作目录：{active.workspace_dir ?? '默认'}
                    </p>
                  </div>
                )}

                {chatMessages.map((message) => (
                  <MessageView key={message.id} message={message} avatar={active.role_avatar ?? undefined} />
                ))}

                {live && live.userText && (
                  <MessageView
                    message={{
                      id: 'live-user',
                      role: 'user',
                      content: live.userText,
                      parts: [],
                      status: 'complete',
                    }}
                  />
                )}

                {liveMessage && <MessageView message={liveMessage} avatar={live?.avatar} />}
              </div>
            </div>

            {error && (
              <div style={{ padding: '0 26px' }}>
                <div className="banner err">{error}</div>
              </div>
            )}

            <div className="composer">
              <div className="composer-inner">
                <div className="composer-box">
                  <textarea
                    ref={textareaRef}
                    rows={1}
                    value={draft}
                    placeholder={busy ? '智能体正在工作…' : `发消息给 ${active.role_name}（Enter 发送，Shift+Enter 换行）`}
                    onChange={(e) => {
                      setDraft(e.target.value)
                      const node = e.target
                      node.style.height = 'auto'
                      node.style.height = `${Math.min(node.scrollHeight, 220)}px`
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                        e.preventDefault()
                        if (!busy) void send(draft)
                      }
                    }}
                    disabled={busy}
                  />
                  {busy ? (
                    <button className="btn danger" onClick={() => void stop()}>
                      ■ 停止
                    </button>
                  ) : (
                    <button className="btn primary" onClick={() => void send(draft)} disabled={!draft.trim()}>
                      发送 ↵
                    </button>
                  )}
                </div>
                {usageSummary.messages > 0 && (
                  <div className="usage-bar" title="整段对话累计消耗">
                    <span className="usage-tag">本对话消耗</span>
                    <span className="usage-item">
                      <span className="usage-label">读取</span>
                      <span className="usage-value">{usageSummary.input.toLocaleString()}</span>
                      <span className="usage-unit">tok</span>
                    </span>
                    <span className="usage-item">
                      <span className="usage-label">输出</span>
                      <span className="usage-value">{usageSummary.output.toLocaleString()}</span>
                      <span className="usage-unit">tok</span>
                    </span>
                    <span className="usage-item">
                      <span className="usage-label">总计</span>
                      <span className="usage-value">{usageSummary.total.toLocaleString()}</span>
                      <span className="usage-unit">tok</span>
                    </span>
                    <span className="usage-item">
                      <span className="usage-label">缓存命中</span>
                      <span className="usage-value">
                        {usageSummary.cacheRatio === null
                          ? '—'
                          : `${Math.round(usageSummary.cacheRatio * 100)}%`}
                      </span>
                    </span>
                    {usageSummary.cacheRead > 0 && (
                      <span className="usage-item usage-muted">
                        <span className="usage-label">命中缓存</span>
                        <span className="usage-value">{usageSummary.cacheRead.toLocaleString()}</span>
                        <span className="usage-unit">tok</span>
                      </span>
                    )}
                  </div>
                )}

                <div className="composer-hint">
                  <span>
                    {active.agent_name} · {active.session_id ? '续用智能体会话' : '新建智能体会话'}
                  </span>
                  <span>所有输出都会写回工作目录</span>
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            <div className="empty-icon">💬</div>
            <p>选择一个对话，或新建一个</p>
            <button className="btn primary" style={{ marginTop: 14 }} onClick={() => setPickerOpen(true)}>
              + 新建对话
            </button>
          </div>
        )}
      </section>

      {pickerOpen && (
        <Modal
          title="选择角色开始对话"
          onClose={() => setPickerOpen(false)}
          footer={
            <button className="btn ghost" onClick={() => setPickerOpen(false)}>
              取消
            </button>
          }
        >
          {roles.length === 0 ? (
            <div className="banner info">还没有角色。请先到「角色」页创建：角色 = 人格设定 + 绑定的智能体。</div>
          ) : (
            <div className="stack" style={{ gap: 8 }}>
              {roles.map((role) => (
                <div key={role.id} className="agent-type-row" onClick={() => void createConversation(role.id)}>
                  <div className="avatar">{role.avatar}</div>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className="agent-type-name">{role.name}</div>
                    <div className="agent-type-desc">
                      {role.agent_name} · {role.tagline ?? '无备注'}
                    </div>
                  </div>
                  <span className="pill">开始</span>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
