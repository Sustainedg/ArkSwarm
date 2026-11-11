import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../../components/MessageView'
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from '../../lib/agentStream'
import { api, streamPost } from '../../lib/api'
import type { Conversation, Message } from '../../lib/types'
import { sameMessageList, useAutoRefresh } from '../../lib/useAutoRefresh'
import { summarizeUsage } from '../../lib/usage'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError, short } from '../util'

interface LiveTurn extends AgentStreamState {
  userText: string
}

/** 对话详情：整屏消息时间线 + 底部输入区，流式输出与桌面端共用一套事件归约。 */
export function MobileChatPage() {
  const { conversationId } = useParams<{ conversationId: string }>()
  const navigate = useNavigate()

  const [active, setActive] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [live, setLive] = useState<LiveTurn | null>(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [remoteRunning, setRemoteRunning] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const open = useCallback(async (id: string) => {
    try {
      const detail = await api.getConversation(id)
      setMessages((prev) => (sameMessageList(prev, detail.messages) ? prev : detail.messages))
      setActive(detail.conversation)
      setLive(null)
      setRemoteRunning(Boolean(detail.active_run))
    } catch (e) {
      setError(describeError(e))
    }
  }, [])

  useEffect(() => {
    if (!conversationId) return
    setError(null)
    void open(conversationId)
  }, [conversationId, open])

  // 跨设备同步：另一台设备写入的消息与运行状态会在这里自动出现。
  useAutoRefresh(() => (conversationId ? open(conversationId) : undefined), {
    intervalMs: 3000,
    enabled: Boolean(conversationId) && !busy,
  })

  // 离开页面（返回或切换对话）时终止正在进行的流，避免后台空转。
  useEffect(() => () => abortRef.current?.abort(), [])

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [messages, live?.text, live?.parts.length])

  const send = async (content: string, regenerate = false) => {
    if (!active) return
    if (remoteRunning) return
    if (!regenerate && !content.trim()) return

    setError(null)
    setBusy(true)
    setRemoteRunning(false)
    setLive({ ...emptyAgentStream(), userText: regenerate ? '' : content })
    if (!regenerate) setDraft('')

    const controller = new AbortController()
    abortRef.current = controller
    const conversationId = active.id

    try {
      await streamPost(
        regenerate
          ? `/api/conversations/${conversationId}/regenerate`
          : `/api/conversations/${conversationId}/messages`,
        regenerate ? {} : { content },
        (event) => setLive((prev) => (prev ? { ...prev, ...applyAgentEvent(prev, event) } : prev)),
        controller.signal,
      )
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') setError(describeError(e))
    } finally {
      abortRef.current = null
      setBusy(false)
      try {
        await open(conversationId)
      } catch {
        /* 刷新失败时保留流式渲染出来的内容 */
      }
    }
  }

  const stop = async () => {
    if (!active) return
    abortRef.current?.abort()
    // 尽力而为：本地流已经断开，服务端取消失败不影响用户（后台会自行超时回收）。
    await api.cancelRun(active.id).catch(() => undefined)
  }

  const resetSession = async () => {
    if (!active) return
    setMenuOpen(false)
    await api.resetSession(active.id)
    await open(active.id)
  }

  const removeConversation = async () => {
    if (!active) return
    setMenuOpen(false)
    if (!confirm(`删除对话「${active.title}」？`)) return
    await api.deleteConversation(active.id)
    navigate('/chat', { replace: true })
  }

  const copyWorkspace = async () => {
    setMenuOpen(false)
    if (!active?.workspace_dir) return
    try {
      await navigator.clipboard.writeText(active.workspace_dir)
    } catch {
      // 剪贴板在非 https/无权限时会失败：明确告诉用户改用长按复制，不要静默无反应。
      setError('复制失败：请长按工作目录手动复制')
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

  const usage = useMemo(
    () => summarizeUsage([...messages.filter((m) => m.role === 'assistant').map((m) => m.usage), live?.usage]),
    [messages, live?.usage],
  )

  return (
    <div className="m-page">
      <MobileTopBar
        onBack={() => navigate('/chat')}
        leading={<span className="avatar">{active?.role_avatar ?? '🤖'}</span>}
        title={active?.role_name ?? '对话'}
        subtitle={
          active
            ? `${active.agent_name ?? ''} · ${active.session_id ? `会话 ${short(active.session_id, 8)}` : '新会话'}`
            : '正在加载…'
        }
        actions={
          <button className="m-icon-btn" onClick={() => setMenuOpen(true)} aria-label="更多操作">
            ⋯
          </button>
        }
      />

      <div className="m-thread" ref={scrollRef}>
        {chatMessages.length === 0 && !liveMessage && active && (
          <div className="m-empty">
            <div className="m-empty-icon">{active.role_avatar ?? '🤖'}</div>
            <p>与「{active.role_name}」的对话已就绪</p>
            <p className="dim small">所有输出都会写回工作目录 {short(active.workspace_dir ?? '默认', 40)}</p>
          </div>
        )}

        {chatMessages.map((message) => (
          <MessageView
            key={message.id}
            message={message}
            avatar={active?.role_avatar ?? undefined}
            label={active?.role_name ?? undefined}
          />
        ))}

        {live?.userText && (
          <MessageView
            message={{ id: 'live-user', role: 'user', content: live.userText, parts: [], status: 'complete' }}
          />
        )}

        {liveMessage && (
          <MessageView message={liveMessage} avatar={active?.role_avatar ?? undefined} label={active?.role_name ?? undefined} />
        )}
      </div>

      {error && (
        <div style={{ padding: '0 12px 8px' }}>
          <div className="banner err">{error}</div>
        </div>
      )}

      {remoteRunning && !busy && (
        <div style={{ padding: '0 12px 8px' }}>
          <div className="banner info">另一台设备正在运行这个对话，完成后会自动同步。</div>
        </div>
      )}

      <div className="m-composer">
        <div className="m-composer-box">
          <textarea
            rows={1}
            value={draft}
            placeholder={
              busy
                ? '智能体正在工作…'
                : remoteRunning
                  ? '另一台设备正在运行…'
                  : `发消息给 ${active?.role_name ?? '智能体'}`
            }
            disabled={busy || remoteRunning}
            onChange={(event) => {
              setDraft(event.target.value)
              const node = event.target
              node.style.height = 'auto'
              node.style.height = `${Math.min(node.scrollHeight, 160)}px`
            }}
          />
          {busy ? (
            <button className="btn danger" onClick={() => void stop()}>
              ■ 停止
            </button>
          ) : (
            <button className="btn primary" onClick={() => void send(draft)} disabled={!draft.trim() || remoteRunning}>
              发送
            </button>
          )}
        </div>
        {usage.messages > 0 && (
          <div className="m-usage">
            <span>
              读取 <b>{usage.input.toLocaleString()}</b> tok
            </span>
            <span>
              输出 <b>{usage.output.toLocaleString()}</b> tok
            </span>
            <span>
              总计 <b>{usage.total.toLocaleString()}</b> tok
            </span>
            <span>
              缓存命中{' '}
              <b>{usage.cacheRatio === null ? '—' : `${Math.round(usage.cacheRatio * 100)}%`}</b>
            </span>
          </div>
        )}
      </div>

      {menuOpen && active && (
        <MobileSheet title="对话操作" onClose={() => setMenuOpen(false)}>
          <div className="stack" style={{ gap: 8 }}>
            <button
              className="btn block"
              disabled={busy || remoteRunning || messages.length === 0}
              onClick={() => {
                setMenuOpen(false)
                void send('', true)
              }}
            >
              ↻ 重新生成最后一条回复
            </button>
            <button className="btn block" disabled={busy || !active.session_id} onClick={() => void resetSession()}>
              ⟲ 重置智能体记忆
            </button>
            <button className="btn block" disabled={!active.workspace_dir} onClick={() => void copyWorkspace()}>
              📋 复制工作目录
            </button>
            <button className="btn block danger" onClick={() => void removeConversation()}>
              删除这个对话
            </button>
          </div>
        </MobileSheet>
      )}
    </div>
  )
}
