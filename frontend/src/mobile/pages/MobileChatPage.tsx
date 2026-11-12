import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../../components/MessageView'
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from '../../lib/agentStream'
import { api, ApiError, streamPost } from '../../lib/api'
import { formatListTime } from '../../lib/datetime'
import { shareConversationImage } from '../../lib/shareImage'
import { useThemeMode } from '../../lib/theme'
import { useSpeechOutput } from '../../lib/tts'
import type { AttachmentRef, Conversation, ConversationDetail, Message } from '../../lib/types'
import { sameMessageList, useAutoRefresh } from '../../lib/useAutoRefresh'
import { summarizeUsage } from '../../lib/usage'
import { PencilIcon, ShareIcon, SpeakerIcon } from '../../components/Icons'
import { Composer } from '../../components/Composer'
import { useMobileMenu } from '../components/MobileMenuButton'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError, short } from '../util'

interface LiveTurn extends AgentStreamState {
  userText: string
}

/**
 * 对话页：移动端打开就是它 —— 直接显示聊天，左上角 logo 打开左侧抽屉（导航 + 对话列表）。
 *
 * - `/chat` 自动进入最近一次对话（一条都没有时给空状态引导）；
 * - `/chat/:conversationId` 打开指定对话。
 * 流式输出与桌面端共用一套事件归约（`lib/agentStream`）。
 *
 * 顶栏右上角只有「分享 + 朗读」两个按钮（朗读默认关闭，开着时每条新回复念出来）；会话级的操作
 * （重新生成 / 重置记忆 / 复制工作目录 / 删除）挪到抽屉里每个对话行的「⋯」上 ——
 * 那几件事要用本页的消息与会话状态，所以点进去还是落到同一个面板。
 */
export function MobileChatPage() {
  const { conversationId } = useParams<{ conversationId: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const openMenu = useMobileMenu()
  const speech = useSpeechOutput()
  const { theme } = useThemeMode()

  const [active, setActive] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [live, setLive] = useState<LiveTurn | null>(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)
  /** 后台同步（轮询）产生的提示：下一次同步成功就撤掉，不长期占着界面 */
  const [syncError, setSyncError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [remoteRunning, setRemoteRunning] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  /** `/chat` 的默认对话还没定下来（或一条对话都没有）时不给空状态，避免闪一下 */
  const [resolving, setResolving] = useState(true)

  /** 已上传、等待随消息发出的附件（输入区共享组件持有 UI，状态由本页持有） */
  const [attachments, setAttachments] = useState<AttachmentRef[]>([])
  /** 正在生成长图（生成要几百毫秒，期间按钮置灰，避免连点出两张） */
  const [sharing, setSharing] = useState(false)
  /** 一次性的提示（分享图已下载之类），与错误分开显示 */
  const [notice, setNotice] = useState<string | null>(null)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  /** 已经念过的回复 id（切换对话时把历史一次性记为「已念过」，不重念） */
  const spokenRef = useRef<Set<string>>(new Set())
  const primedConversationRef = useRef<string | null>(null)
  /** 本端刚删掉的对话 id：它引出的 404 是预期行为，不该再当成「这条对话不见了」处理 */
  const selfDeletedRef = useRef<string | null>(null)

  const applyDetail = useCallback((detail: ConversationDetail, resetLive: boolean) => {
    setMessages((prev) => (sameMessageList(prev, detail.messages) ? prev : detail.messages))
    setActive(detail.conversation)
    if (resetLive) setLive(null)
    setRemoteRunning(Boolean(detail.active_run))
  }, [])

  const open = useCallback(
    async (id: string) => {
      try {
        const detail = await api.getConversation(id)
        applyDetail(detail, true)
        return detail
      } catch (e) {
        setError(describeError(e))
        return null
      }
    },
    [applyDetail],
  )

  /** 后台同步（每 3 秒）：只更新数据，不覆盖用户主动操作产生的提示。 */
  const syncConversation = useCallback(
    async (id: string) => {
      try {
        applyDetail(await api.getConversation(id), false)
        setSyncError(null)
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) {
          // 本端刚删的那条：在途的轮询打回来必然是 404，属于预期（删除流程自己会跳走）。
          if (selfDeletedRef.current === id) return
          // 在别的设备上被删了：回 /chat 重新落到剩下的最近一条，别留一条消不掉的 404。
          setError(null)
          setSyncError(null)
          setActive(null)
          setMessages([])
          navigate('/chat', { replace: true })
          return
        }
        setSyncError(describeError(e))
      }
    },
    [applyDetail, navigate],
  )

  useEffect(() => {
    // 回到 `/chat` 时先重新解析默认对话，避免刚删掉的那条留下一个空状态闪一下。
    if (!conversationId) setResolving(true)
    void (async () => {
      if (conversationId) {
        setError(null)
        await open(conversationId)
        setResolving(false)
        return
      }
      // 默认直接显示聊天界面：对话列表在左侧抽屉里，这里先进入最近一次对话。
      try {
        const list = await api.listConversations()
        if (list.length > 0) navigate(`/chat/${list[0].id}`, { replace: true })
        else {
          setActive(null)
          setMessages([])
        }
      } catch (e) {
        setError(describeError(e))
      } finally {
        setResolving(false)
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  // 抽屉里对话行的「⋯」：先落到这个对话，再把会话操作面板打开。
  // 用一次性的路由 state 传信号，读完（replace 掉）就清空，返回 / 刷新不会又弹一次。
  useEffect(() => {
    const state = location.state as { openActions?: boolean } | null
    if (!state?.openActions) return
    setMenuOpen(true)
    navigate(location.pathname, { replace: true, state: null })
  }, [location.state, location.pathname, navigate])

  // 跨设备同步：另一台设备写入的消息与运行状态会在这里自动出现。
  useAutoRefresh(() => (conversationId ? syncConversation(conversationId) : undefined), {
    intervalMs: 3000,
    enabled: Boolean(conversationId) && !busy,
  })

  /**
   * 朗读：开关开着 + 这条回复还没念过才出声，一次只念**最新**那条（新的会打断旧的）。
   *
   * 统一在这里判「新回复」，而不是在 `send()` 里 —— 这样另一台设备写入、由
   * `useAutoRefresh` 拉回来的回复也会念。切换对话时先把已有消息记成「已念过」，
   * 打开历史对话不会从头念一遍。
   */
  useEffect(() => {
    const replies = messages.filter((message) => message.role === 'assistant' && message.status !== 'error')
    if (primedConversationRef.current !== (active?.id ?? null)) {
      primedConversationRef.current = active?.id ?? null
      spokenRef.current = new Set(replies.map((message) => message.id))
      return
    }
    const fresh = replies.filter((message) => !spokenRef.current.has(message.id))
    fresh.forEach((message) => spokenRef.current.add(message.id))
    if (fresh.length > 0) speech.speakIfEnabled(fresh[fresh.length - 1].content)
  }, [messages, active?.id, speech.speakIfEnabled])

  // 一次性提示（分享图已下载 / 朗读已开启）看完就撤，不长期占着输入框上方。
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  // 离开页面（返回或切换对话）时终止正在进行的流，避免后台空转。
  // （语音识别由输入区共享组件在自己卸载时收起。）
  useEffect(() => () => abortRef.current?.abort(), [])

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [messages, live?.text, live?.parts.length])

  const send = async (content: string, regenerate = false) => {
    if (!active) return
    if (remoteRunning) return
    const pending = regenerate ? [] : attachments
    if (!regenerate && !content.trim() && pending.length === 0) return

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
        regenerate ? {} : { content, attachments: pending.map((item) => item.rel_path) },
        (event) => setLive((prev) => (prev ? { ...prev, ...applyAgentEvent(prev, event) } : prev)),
        controller.signal,
      )
      // 消息已经落到服务端，附件随之清空；失败时保留，方便用户重发。
      if (pending.length > 0) setAttachments([])
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

  /** 输入区共享组件把读好的 base64 交回来，这里只负责调接口。 */
  const uploadAttachment = async (name: string, contentBase64: string): Promise<AttachmentRef> => {
    if (!active) throw new Error('先选一段对话，再添加附件')
    return api.uploadAttachment(active.id, { name, content_base64: contentBase64 })
  }

  const stop = async () => {
    if (!active) return
    abortRef.current?.abort()
    // 尽力而为：本地流已经断开，服务端取消失败不影响用户（后台会自行超时回收）。
    await api.cancelRun(active.id).catch(() => undefined)
  }

  const shareChat = async () => {
    if (!active || messages.length === 0) return
    setSharing(true)
    setError(null)
    setNotice(null)
    try {
      const outcome = await shareConversationImage({
        title: active.title,
        subtitle: [active.role_name, active.agent_name].filter(Boolean).join(' · '),
        theme,
        messages: messages.map((message) => ({
          role: message.role,
          content: message.content,
          createdAt: formatListTime(message.created_at),
        })),
      })
      if (outcome === 'downloaded') setNotice('分享图已生成，已开始下载（当前环境不支持系统分享面板）')
    } catch (e) {
      setError(describeError(e))
    } finally {
      setSharing(false)
    }
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
    try {
      await api.deleteConversation(active.id)
    } catch (e) {
      setError(describeError(e))
      return
    }
    selfDeletedRef.current = active.id
    setError(null)
    setSyncError(null)
    setActive(null)
    setMessages([])
    // 回到 /chat：由它自动进入剩下的最近一次对话（一条都没有时给空状态）。
    navigate('/chat', { replace: true })
  }

  const renameConversation = async () => {
    if (!active) return
    setMenuOpen(false)
    const title = prompt('对话名称', active.title)?.trim()
    if (!title || title === active.title) return
    try {
      await api.updateConversation(active.id, { title })
      await open(active.id)
    } catch (e) {
      setError(describeError(e))
    }
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
        title={active?.role_name ?? 'Magenta Agents'}
        subtitle={
          active
            ? `${active.agent_name ?? ''} · ${active.session_id ? `会话 ${short(active.session_id, 8)}` : '新会话'}`
            : resolving
              ? '正在加载…'
              : '多智能体控制台'
        }
        actions={
          <>
            <button
              className="m-icon-btn"
              onClick={() => void shareChat()}
              disabled={!active || messages.length === 0 || sharing}
              aria-label="分享对话"
              title="把这段对话生成长图分享出去"
            >
              <ShareIcon />
            </button>
            <button
              className={`m-icon-btn${speech.enabled ? ' on' : ''}`}
              onClick={() => {
                const next = !speech.enabled
                speech.setEnabled(next)
                setNotice(next ? '朗读已开启：新回复会自动念出来' : '朗读已关闭')
              }}
              disabled={!speech.supported}
              aria-pressed={speech.enabled}
              aria-label={speech.enabled ? '关闭朗读' : '开启朗读'}
              title={
                !speech.supported
                  ? '当前浏览器不支持朗读，可用系统自带的读屏'
                  : speech.enabled
                    ? '朗读已开启：新回复会自动念出来，点一下关闭'
                    : '朗读已关闭：点一下开启，新回复会自动念出来'
              }
            >
              <SpeakerIcon on={speech.enabled} />
            </button>
          </>
        }
      />

      <div className="m-thread" ref={scrollRef}>
        {!active && !resolving && (
          <div className="m-empty">
            <div className="m-empty-icon">💬</div>
            <p>还没有对话</p>
            <p className="dim small">打开左上角的菜单，选一个角色开始第一句。</p>
            <button className="btn sm primary" style={{ marginTop: 6 }} onClick={openMenu}>
              打开菜单
            </button>
          </div>
        )}

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

      {(error ?? syncError) && (
        <div style={{ padding: '0 12px 8px' }}>
          <div className="banner err">{error ?? syncError}</div>
        </div>
      )}

      {notice && (
        <div style={{ padding: '0 12px 8px' }}>
          <div className="banner info">{notice}</div>
        </div>
      )}

      {remoteRunning && !busy && (
        <div style={{ padding: '0 12px 8px' }}>
          <div className="banner info">另一台设备正在运行这个对话，完成后会自动同步。</div>
        </div>
      )}

      <Composer
        value={draft}
        onChange={setDraft}
        onSend={() => void send(draft)}
        attachments={attachments}
        onAttachmentsChange={setAttachments}
        onUpload={uploadAttachment}
        onStop={() => void stop()}
        busy={busy}
        disabled={!active || remoteRunning}
        onNotice={setError}
        // token 用量在输入框「上方」：输入框要贴着屏幕底部，中间不留空档。
        header={
          usage.messages > 0 ? (
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
                缓存命中 <b>{usage.cacheRatio === null ? '—' : `${Math.round(usage.cacheRatio * 100)}%`}</b>
              </span>
            </div>
          ) : null
        }
      />

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
            <button className="btn block" onClick={() => void renameConversation()}>
              <PencilIcon size={15} /> 重命名这个对话
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
