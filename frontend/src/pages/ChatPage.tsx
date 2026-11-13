import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { Composer } from '../components/Composer'
import { ConversationThread, type ConversationThreadItem } from '../components/ConversationThread'
import { AgentIcon, FolderIcon, ModelIcon, ShareIcon, SpeakerIcon } from '../components/Icons'
import type { RenderMessage } from '../components/MessageView'
import { Modal } from '../components/Modal'
import { applyAgentEvent, emptyAgentStream, type AgentStreamState } from '../lib/agentStream'
import { api, ApiError, streamGet, streamPost } from '../lib/api'
import { notifyConversationsChanged } from '../lib/conversations'
import { formatListTime } from '../lib/datetime'
import { shortPath } from '../lib/paths'
import { shareConversationImage } from '../lib/shareImage'
import { useThemeMode } from '../lib/theme'
import { useSpeechOutput } from '../lib/tts'
import type {
  ActiveRunInfo,
  AgentEvent,
  AgentProfile,
  AttachmentRef,
  Conversation,
  ConversationDetail,
  ConversationModelOptions,
  Message,
} from '../lib/types'
import { sameMessageList, useAutoRefresh } from '../lib/useAutoRefresh'
import { summarizeUsage } from '../lib/usage'

interface LiveTurn extends AgentStreamState {
  userText: string
  agentName: string
  avatar: string
}

/**
 * 对话页（桌面端）：左侧栏（导航 + 对话列表，见 `App.tsx`）选中一条对话，这里显示它。
 *
 * 与移动端同一套设计与分工：
 * - 顶栏右上角只有「分享 + 朗读」两个图标按钮（朗读默认关闭，开着时每条新回复念出来）；
 * - 输入区是两端共享的 `components/Composer`（附件 / 语音 / 图标发送都在框里）；
 * - 会话级操作（重新生成 / 重置记忆 / 复制工作目录 / 删除）不占顶栏，收在左侧栏对话行的
 *   「⋯」后面 —— 那几件事要用到本页的消息与会话状态，所以经路由 state 落回本页的弹窗。
 */
export function ChatPage() {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  const speech = useSpeechOutput()
  const { theme } = useThemeMode()

  const [messages, setMessages] = useState<Message[]>([])
  const [active, setActive] = useState<Conversation | null>(null)
  const [live, setLive] = useState<LiveTurn | null>(null)
  const [draft, setDraft] = useState('')
  /** 已上传、等待随消息发出的附件（输入区共享组件持有 UI，状态由本页持有） */
  const [attachments, setAttachments] = useState<AttachmentRef[]>([])
  const [error, setError] = useState<string | null>(null)
  /** 后台同步（轮询）产生的提示：下一次同步成功就撤掉，不长期占着界面 */
  const [syncError, setSyncError] = useState<string | null>(null)
  /** 一次性的提示（分享图已下载之类），与错误分开显示 */
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [remoteRunning, setRemoteRunning] = useState(false)
  /** 这条对话当前正在跑的运行（另一台设备 / 刷新后重连时据此订阅实时输出） */
  const [activeRun, setActiveRun] = useState<ActiveRunInfo | null>(null)
  /** 强制重新订阅的计数器：同一轮运行被 openConversation 清掉实时卡片后要能重连 */
  const [attachNonce, setAttachNonce] = useState(0)
  /** 会话操作弹窗（从左侧栏对话行的「⋯」打开） */
  const [actionsOpen, setActionsOpen] = useState(false)
  /** 正在生成长图（要几百毫秒，期间按钮置灰避免连点出两张） */
  const [sharing, setSharing] = useState(false)
  /** 输入框上方那几个小图标打开的面板：工作目录 / 智能体 / 模型 */
  const [scopePanel, setScopePanel] = useState<'workspace' | 'agent' | 'model' | null>(null)
  const [scopeAgents, setScopeAgents] = useState<AgentProfile[]>([])
  /** 面板里最近用过的几个工作目录（从对话列表里挑，省得手打长路径） */
  const [scopeRecent, setScopeRecent] = useState<string[]>([])
  const [workspaceDraft, setWorkspaceDraft] = useState('')
  const [scopeSaving, setScopeSaving] = useState(false)
  /** 换模型面板：可选清单 / 当前填写的值 */
  const [modelOptions, setModelOptions] = useState<ConversationModelOptions | null>(null)
  const [modelDraft, setModelDraft] = useState('')

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  /** 已经念过的回复 id（切换对话时把历史一次性记为「已念过」，不重念） */
  const spokenRef = useRef<Set<string>>(new Set())
  const primedConversationRef = useRef<string | null>(null)
  /** 本端刚删掉的对话 id：它引出的 404 是预期行为，不该再当成「这条对话不见了」处理 */
  const selfDeletedRef = useRef<string | null>(null)
  /** 正在订阅的另一台设备 / 后台运行 */
  const attachRef = useRef<AbortController | null>(null)
  const attachedRunIdRef = useRef<string | null>(null)
  const attachedLiveRef = useRef(false)
  const activeRef = useRef<Conversation | null>(null)
  activeRef.current = active
  /** 消息列表是否贴着底部：用户往上翻看历史时，流式输出不能把人拽回底部 */
  const stickRef = useRef(true)
  const stickConversationRef = useRef<string | null>(null)

  const applyDetail = useCallback((detail: ConversationDetail, resetLive: boolean) => {
    setMessages((prev) => (sameMessageList(prev, detail.messages) ? prev : detail.messages))
    setActive(detail.conversation)
    if (resetLive) {
      // 主动重开 / 刷新这条对话：旧的实时卡片先撤掉，若仍在运行则让订阅 effect 重新连一次。
      setLive(null)
      attachRef.current?.abort()
      attachedRunIdRef.current = null
      attachedLiveRef.current = false
      setAttachNonce((value) => value + 1)
    }
    setActiveRun(detail.active_run ?? null)
    setRemoteRunning(Boolean(detail.active_run))
  }, [])

  const openConversation = useCallback(
    async (id: string) => {
      // 明确要打开某条对话（点击 / 路由 / 重置记忆）：先撤掉上一次的提示，再按本次结果决定报不报错。
      setError(null)
      setSyncError(null)
      try {
        applyDetail(await api.getConversation(id), true)
      } catch (e) {
        setError(describe(e))
      }
    },
    [applyDetail],
  )

  /** 正在看的这条对话已经不在了（多半是在别的设备上被删）：落到剩下的对话，别留一条消不掉的 404。 */
  const leaveMissingConversation = useCallback(async () => {
    setError(null)
    setSyncError(null)
    try {
      const list = await api.listConversations()
      if (list.length > 0) navigate(`/chat/${list[0].id}`, { replace: true })
      else {
        setActive(null)
        setMessages([])
        navigate('/chat', { replace: true })
      }
    } catch (e) {
      setSyncError(describe(e))
    }
  }, [navigate])

  useEffect(() => {
    void (async () => {
      if (conversationId) {
        await openConversation(conversationId)
        return
      }
      // `/chat`：进入最近一次对话（一条都没有时给空状态）。
      setError(null)
      try {
        const list = await api.listConversations()
        if (list.length > 0) navigate(`/chat/${list[0].id}`, { replace: true })
        else {
          setActive(null)
          setMessages([])
        }
      } catch (e) {
        setError(describe(e))
      }
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId])

  // 左侧栏对话行的「⋯」：先落到这个对话，再把会话操作弹窗打开。
  // 用一次性的路由 state 传信号，读完（replace 掉）就清空，返回 / 刷新不会又弹一次。
  useEffect(() => {
    const state = location.state as { openActions?: boolean } | null
    if (!state?.openActions) return
    setActionsOpen(true)
    navigate(location.pathname, { replace: true, state: null })
  }, [location.state, location.pathname, navigate])

  // 流式输出时只在下沿跟随；用户把滚轮往上翻看历史，就停在原处，不再被拽回底部。
  // 切换对话时强制回到底部（新对话从最新一条看起）。
  useEffect(() => {
    const node = scrollRef.current
    if (!node) return
    if (stickConversationRef.current !== (conversationId ?? null)) {
      stickConversationRef.current = conversationId ?? null
      stickRef.current = true
      node.scrollTop = node.scrollHeight
      return
    }
    if (stickRef.current) node.scrollTop = node.scrollHeight
  }, [messages, live?.text, live?.parts.length, conversationId])

  const onThreadScroll = () => {
    const node = scrollRef.current
    if (!node) return
    // 离底部 80px 以内算「还在跟随」，给触屏和鼠标滚轮都留一点余量。
    stickRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80
  }

  const refreshActive = useCallback(async () => {
    if (!conversationId) return
    try {
      applyDetail(await api.getConversation(conversationId), false)
      setSyncError(null)
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        // 本端刚删的那条：在途的轮询打回来必然是 404，属于预期，不提示也不跳转（删除流程自己会跳）。
        if (selfDeletedRef.current === conversationId) return
        await leaveMissingConversation()
        return
      }
      setSyncError(describe(e))
    }
  }, [conversationId, applyDetail, leaveMissingConversation])

  // 跨设备同步：另一台设备发出的消息与最后状态会在这里自动出现。
  useAutoRefresh(refreshActive, { intervalMs: 3000, enabled: Boolean(conversationId) && !busy })

  /**
   * 另一台设备 / 刷新后的实时重连：详情里带 `active_run` 就订阅那条事件流。
   *
   * 后端把运行与 HTTP 连接解耦了，所以这里只是「订阅者」：页面刷新不会终止运行，
   * 重新进来会先重放缓冲里的事件，再接着收实时增量，和本地发消息的体验一致。
   */
  useEffect(() => {
    if (!conversationId || busy || !activeRun) return
    if (attachedRunIdRef.current === activeRun.id) return

    const runId = activeRun.id
    const conversation = activeRef.current
    const controller = new AbortController()
    attachRef.current = controller
    attachedRunIdRef.current = runId
    attachedLiveRef.current = true
    setLive({
      ...emptyAgentStream(),
      // 用户消息已经在数据库里（详情会拉到），这里只渲染正在生成的助手回复。
      userText: '',
      agentName: conversation?.agent_name ?? activeRun.agent_name ?? '智能体',
      avatar: conversation?.agent_avatar ?? '🤖',
    })

    let cancelled = false
    void streamGet<AgentEvent>(
      `/api/conversations/${conversationId}/stream`,
      (event) => setLive((prev) => (prev ? { ...prev, ...applyAgentEvent(prev, event) } : prev)),
      controller.signal,
    )
      .catch((e) => {
        if (cancelled || (e as Error)?.name === 'AbortError') return
        // 运行刚好结束（订阅接口 404）：退回普通同步，下一轮轮询给出最终消息。
        if (e instanceof ApiError && e.status === 404) return
        setSyncError(describe(e))
      })
      .finally(() => {
        if (cancelled) return
        attachedLiveRef.current = false
        attachRef.current = null
        attachedRunIdRef.current = null
        setLive(null)
        setActiveRun(null)
        setRemoteRunning(false)
        void refreshActive()
        notifyConversationsChanged()
      })

    return () => {
      cancelled = true
      controller.abort()
      if (attachRef.current === controller) attachRef.current = null
      if (attachedRunIdRef.current === runId) attachedRunIdRef.current = null
      // 切换对话 / 运行结束：实时卡片让位给数据库里的最终消息。
      if (attachedLiveRef.current) {
        attachedLiveRef.current = false
        setLive(null)
      }
    }
  }, [conversationId, activeRun?.id, busy, refreshActive, attachNonce])

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

  // 一次性提示看完就撤，不长期占着输入框上方。
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  // 离开页面（切换对话 / 返回）时终止本端正在读的流，避免后台空转。
  // 注意：后端已经和连接解耦，终止的只是「读取」，不是那一轮运行本身。
  // （语音识别由输入区共享组件在自己卸载时收起。）
  useEffect(() => () => {
    abortRef.current?.abort()
    attachRef.current?.abort()
  }, [])

  const send = async (content: string, regenerate = false) => {
    const conversation = active
    if (!conversation) return
    if (remoteRunning) return
    const pending = regenerate ? [] : attachments
    if (!regenerate && !content.trim() && pending.length === 0) return

    setError(null)
    setBusy(true)
    setRemoteRunning(false)
    setActiveRun(null)
    attachRef.current?.abort()
    attachedRunIdRef.current = null
    attachedLiveRef.current = false
    // 主动发言：无论之前翻到哪里，都回到下沿看这次输出。
    stickRef.current = true
    setLive({
      ...emptyAgentStream(),
      userText: regenerate ? '' : content,
      agentName: conversation.agent_name ?? '智能体',
      avatar: conversation.agent_avatar ?? '🤖',
    })
    if (!regenerate) setDraft('')

    const controller = new AbortController()
    abortRef.current = controller

    try {
      await streamPost(
        regenerate
          ? `/api/conversations/${conversation.id}/regenerate`
          : `/api/conversations/${conversation.id}/messages`,
        regenerate ? {} : { content, attachments: pending.map((item) => item.rel_path) },
        (event) => setLive((prev) => (prev ? { ...prev, ...applyAgentEvent(prev, event) } : prev)),
        controller.signal,
      )
      // 消息已经落到服务端，附件随之清空；失败时保留，方便用户重发。
      if (pending.length > 0) setAttachments([])
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') setError(describe(e))
    } finally {
      abortRef.current = null
      setBusy(false)
      try {
        applyDetail(await api.getConversation(conversation.id), true)
      } catch {
        /* 刷新失败时保留流式渲染出来的内容 */
      }
      // 这一轮会让条数与时间变化，左侧栏列表立刻对齐（不用等下一次轮询）。
      notifyConversationsChanged()
    }
  }

  const stop = async () => {
    if (!active) return
    abortRef.current?.abort()
    await api.cancelRun(active.id).catch(() => undefined)
  }

  /** 输入区共享组件把读好的 base64 交回来，这里只负责调接口。 */
  const uploadAttachment = async (name: string, contentBase64: string): Promise<AttachmentRef> => {
    if (!active) throw new Error('先选一段对话，再添加附件')
    return api.uploadAttachment(active.id, { name, content_base64: contentBase64 })
  }

  const shareChat = async () => {
    if (!active || messages.length === 0) return
    setSharing(true)
    setError(null)
    setNotice(null)
    try {
      const outcome = await shareConversationImage({
        title: active.title,
        subtitle: [active.agent_name, active.agent_type_id].filter(Boolean).join(' · '),
        theme,
        messages: messages.map((message) => ({
          role: message.role,
          content: message.content,
          createdAt: formatListTime(message.created_at),
        })),
      })
      if (outcome === 'downloaded') setNotice('分享图已生成，已开始下载（当前环境不支持系统分享面板）')
    } catch (e) {
      setError(describe(e))
    } finally {
      setSharing(false)
    }
  }

  const resetSession = async () => {
    if (!active) return
    setActionsOpen(false)
    await api.resetSession(active.id)
    await openConversation(active.id)
  }

  const removeConversation = async () => {
    if (!active) return
    setActionsOpen(false)
    if (!confirm(`删除对话「${active.title}」？`)) return
    try {
      await api.deleteConversation(active.id)
    } catch (e) {
      setError(describe(e))
      return
    }
    selfDeletedRef.current = active.id
    setError(null)
    setSyncError(null)
    setAttachments([])
    notifyConversationsChanged()
    // 回到 /chat：由它自动进入剩下的最近一次对话（一条都没有时给空状态）。
    navigate('/chat', { replace: true })
  }

  const copyWorkspace = async () => {
    setActionsOpen(false)
    if (!active?.workspace_dir) return
    try {
      await navigator.clipboard.writeText(active.workspace_dir)
    } catch {
      // 剪贴板在非 https/无权限时会失败：明确告诉用户改用手动复制，不要静默无反应。
      setError('复制失败：请手动选中工作目录再复制')
    }
  }

  /** 打开「工作目录」面板：顺手把别人用过的目录捞出来当快捷选项。 */
  const openWorkspacePanel = () => {
    if (!active) return
    setWorkspaceDraft(active.workspace_dir ?? '')
    setScopePanel('workspace')
    void api
      .listConversations()
      .then((list) =>
        setScopeRecent(
          [
            ...new Set(
              list
                .map((item) => item.workspace_dir)
                .filter((item): item is string => Boolean(item)),
            ),
          ].slice(0, 5),
        ),
      )
      .catch(() => setScopeRecent([]))
  }

  const openAgentPanel = () => {
    setScopePanel('agent')
    void api
      .listAgents()
      .then(setScopeAgents)
      .catch((e) => setError(describe(e)))
  }

  /**
   * 改这条对话的「工作目录」或「智能体」。
   *
   * 两件事都在后端会清空会话记忆（内核的会话是绑在智能体 + 工作目录上的），所以：
   * 点中的就是当前值 → 什么都不做，免得白丢一次记忆。
   */
  const applyScope = async (patch: { workspace_dir?: string; agent_profile_id?: string }) => {
    if (!active) return
    const sameAgent = patch.agent_profile_id === undefined || patch.agent_profile_id === active.agent_profile_id
    const sameWorkspace =
      patch.workspace_dir === undefined || patch.workspace_dir === (active.workspace_dir ?? '')
    if (sameAgent && sameWorkspace) {
      setScopePanel(null)
      return
    }
    setScopeSaving(true)
    setError(null)
    try {
      await api.updateConversation(active.id, patch)
      setScopePanel(null)
      await openConversation(active.id)
      notifyConversationsChanged()
    } catch (e) {
      setError(describe(e))
    } finally {
      setScopeSaving(false)
    }
  }

  /**
   * 改这条对话使用的模型。
   *
   * 只影响这条对话的下一轮运行，不重置会话记忆（与换智能体 / 换工作目录不同）。
   * 模型清单来自后端：自定义网关只给白名单，官方默认链路允许直接填写模型名。
   */
  const openModelPanel = () => {
    if (!active) return
    setModelDraft(active.model ?? '')
    setModelOptions(null)
    setScopePanel('model')
    void api
      .getConversationModels(active.id)
      .then((options) => {
        setModelOptions(options)
        setModelDraft(options.current ?? active.model ?? '')
      })
      .catch((e) => setError(describe(e)))
  }

  const applyModel = async (value?: string) => {
    if (!active) return
    const next = (value ?? modelDraft).trim()
    if (next === (active.model ?? '')) {
      setScopePanel(null)
      return
    }
    setScopeSaving(true)
    setError(null)
    try {
      await api.updateConversation(active.id, { model: next || null })
      setScopePanel(null)
      await openConversation(active.id)
      notifyConversationsChanged()
    } catch (e) {
      setError(describe(e))
    } finally {
      setScopeSaving(false)
    }
  }

  // 重连时数据库里那条「正在生成」的空占位要藏起来：同一段内容由实时卡片渲染，
  // 否则会先出现一个空气泡、跑完又换成正式消息。
  const livePlaceholderId = live ? activeRun?.assistant_message_id ?? null : null
  const chatMessages: RenderMessage[] = messages
    .filter(
      (message) =>
        !(
          live &&
          ((livePlaceholderId !== null && message.id === livePlaceholderId) ||
            (livePlaceholderId === null && message.role === 'assistant' && message.status === 'streaming'))
        ),
    )
    .map((message) => ({
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

  const threadItems: ConversationThreadItem[] = [
    ...chatMessages.map((message) => ({
      id: message.id,
      message,
      avatar: active?.agent_avatar ?? undefined,
    })),
    ...(live?.userText
      ? [{
          id: 'live-user',
          message: {
            id: 'live-user',
            role: 'user' as const,
            content: live.userText,
            parts: [],
            status: 'complete',
          },
        }]
      : []),
    ...(liveMessage ? [{ id: liveMessage.id, message: liveMessage, avatar: live?.avatar }] : []),
  ]

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
      <section className="chat-pane">
        {active ? (
          <>
            <div className="chat-topbar">
              <div className="avatar">{active.agent_avatar}</div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600 }}>{active.agent_name}</div>
                <div className="dim small">
                  {active.agent_type_id ?? '智能体'} · {active.session_id ? `会话 ${active.session_id.slice(0, 8)}` : '新会话'}
                </div>
              </div>
              <div className="spacer" style={{ flex: 1 }} />
              <button
                className="icon-btn"
                onClick={() => void shareChat()}
                disabled={messages.length === 0 || sharing}
                aria-label="分享对话"
                title="把这段对话生成长图分享出去"
              >
                <ShareIcon size={18} />
              </button>
              <button
                className={`icon-btn${speech.enabled ? ' on' : ''}`}
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
                <SpeakerIcon on={speech.enabled} size={18} />
              </button>
            </div>

            <div className="chat-scroll" ref={scrollRef} onScroll={onThreadScroll}>
              <ConversationThread
                centered
                items={threadItems}
                empty={chatMessages.length === 0 && !liveMessage ? (
                  <div className="empty">
                    <div className="empty-icon">{active.agent_avatar}</div>
                    <p>与「{active.agent_name}」的对话已就绪</p>
                    <p className="dim small" style={{ marginTop: 6 }}>
                      内核：{active.agent_type_id ?? '智能体'} · 工作目录：{active.workspace_dir ?? '默认'}
                    </p>
                  </div>
                ) : undefined}
              />
            </div>

            {(error ?? syncError) && (
              <div style={{ padding: '0 26px' }}>
                <div className="banner err">{error ?? syncError}</div>
              </div>
            )}

            {notice && (
              <div style={{ padding: '0 26px' }}>
                <div className="banner info">{notice}</div>
              </div>
            )}

            {remoteRunning && !busy && !live && (
              <div style={{ padding: '8px 26px 0' }}>
                <div className="banner info">正在连接另一台设备的实时输出…</div>
              </div>
            )}

            <div className="chat-composer">
              <div className="chat-composer-inner">
                {/* 输入框上方是「工作目录 + 智能体」两个小图标（新建对话时不用先选，这里随时改）；
                    用量统计挪到输入框下面 —— 贴底的是输入框。 */}
                <Composer
                  value={draft}
                  onChange={setDraft}
                  onSend={() => void send(draft)}
                  attachments={attachments}
                  onAttachmentsChange={setAttachments}
                  onUpload={uploadAttachment}
                  onStop={() => void stop()}
                  busy={busy}
                  disabled={remoteRunning}
                  submitOnEnter
                  inputRef={inputRef}
                  onNotice={setError}
                  inputLabel={`发消息给 ${active.agent_name}`}
                  header={
                    <div className="composer-scope">
                      <button
                        type="button"
                        className="composer-scope-btn"
                        onClick={openWorkspacePanel}
                        title={`工作目录：${active.workspace_dir ?? '按对话自动分配'}（点一下改）`}
                      >
                        <FolderIcon size={15} />
                        <span>{shortPath(active.workspace_dir)}</span>
                      </button>
                      <button
                        type="button"
                        className="composer-scope-btn"
                        onClick={openAgentPanel}
                        title={`智能体：${active.agent_name ?? '未知'}（点一下换）`}
                      >
                        <AgentIcon size={15} />
                        <span>{active.agent_name}</span>
                      </button>
                      <button
                        type="button"
                        className="composer-scope-btn"
                        onClick={openModelPanel}
                        title={`模型：${active.model ?? '跟随智能体默认'}（点一下换）`}
                      >
                        <ModelIcon size={15} />
                        <span>{active.model ?? '默认模型'}</span>
                      </button>
                    </div>
                  }
                  footer={
                    usageSummary.messages > 0 ? (
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
                    ) : null
                  }
                />
              </div>
            </div>
          </>
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            <div className="empty-icon">💬</div>
            <p>在左侧选一条对话，或点「＋ 新建对话」开始</p>
          </div>
        )}
      </section>

      {scopePanel === 'workspace' && active && (
        <Modal
          title="工作目录"
          onClose={() => setScopePanel(null)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setScopePanel(null)}>
                取消
              </button>
              <button
                className="btn primary"
                disabled={scopeSaving}
                onClick={() => void applyScope({ workspace_dir: workspaceDraft.trim() })}
              >
                {scopeSaving ? '保存中…' : '保存'}
              </button>
            </>
          }
        >
          <div className="stack" style={{ gap: 10 }}>
            <label className="field">
              <span className="field-label">路径</span>
              <input
                className="input mono"
                autoFocus
                value={workspaceDraft}
                onChange={(event) => setWorkspaceDraft(event.target.value)}
                placeholder="例如 /vol1/projects/my-app"
              />
            </label>
            <p className="field-help">
              留空 = 按这条对话自动分配一个独立沙箱目录。智能体就在这个目录里读写文件，
              换目录会重置这条对话的记忆（会话重新开始）。
            </p>
            {scopeRecent.length > 0 && (
              <div className="stack" style={{ gap: 6 }}>
                <span className="field-help">最近用过</span>
                <div className="chips">
                  {scopeRecent.map((path) => (
                    <button
                      key={path}
                      type="button"
                      className="chip-btn mono-sm"
                      onClick={() => setWorkspaceDraft(path)}
                      title={path}
                    >
                      {shortPath(path)}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <p className="field-help">当前：{active.workspace_dir ?? '按对话自动分配'}</p>
          </div>
        </Modal>
      )}

      {scopePanel === 'agent' && active && (
        <Modal
          title="换智能体"
          onClose={() => setScopePanel(null)}
          footer={
            <button className="btn ghost" onClick={() => setScopePanel(null)}>
              关闭
            </button>
          }
        >
          <div className="stack" style={{ gap: 6 }}>
            <p className="field-help">
              换智能体会重置这条对话的记忆：不同内核的会话不通用，换了就从头开始。
            </p>
            {scopeAgents.length === 0 && (
              <p className="dim small">正在加载智能体…（一个都没有的话，先到「智能体」页创建）</p>
            )}
            {scopeAgents.map((agent) => {
              const current = agent.id === active.agent_profile_id
              return (
                <button
                  key={agent.id}
                  type="button"
                  className={`agent-pick${current ? ' active' : ''}`}
                  disabled={scopeSaving || !agent.enabled}
                  onClick={() => void applyScope({ agent_profile_id: agent.id })}
                  title={agent.enabled ? undefined : '这个智能体已停用'}
                >
                  <span className="avatar" style={{ width: 30, height: 30, fontSize: 16 }}>
                    {agent.avatar}
                  </span>
                  <span className="agent-pick-main">
                    <span className="agent-pick-name">
                      {agent.name}
                      {agent.is_default && <span className="pill brand">默认</span>}
                    </span>
                    <span className="card-sub">
                      {agent.type_name ?? agent.type_id} ·{' '}
                      {agent.gateway_kind === 'official' || !agent.gateway_name
                        ? '官方默认'
                        : agent.gateway_name}{' '}
                      · {agent.model ?? '默认模型'}
                    </span>
                  </span>
                  {current && <span className="pill ok">当前</span>}
                </button>
              )
            })}
          </div>
        </Modal>
      )}

      {scopePanel === 'model' && active && (
        <Modal
          title="模型"
          onClose={() => setScopePanel(null)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setScopePanel(null)}>
                取消
              </button>
              <button className="btn primary" disabled={scopeSaving} onClick={() => void applyModel()}>
                {scopeSaving ? '保存中…' : '保存'}
              </button>
            </>
          }
        >
          <div className="stack" style={{ gap: 10 }}>
            <p className="field-help">
              只影响这条对话的下一轮运行；换模型不会重置会话记忆。
            </p>
            {modelOptions === null ? (
              <p className="dim small">正在加载可选模型…</p>
            ) : modelOptions.models.length > 0 ? (
              <div className="stack" style={{ gap: 6 }}>
                <button
                  type="button"
                  className={`agent-pick${modelDraft ? '' : ' active'}`}
                  onClick={() => setModelDraft('')}
                >
                  <span className="agent-pick-main">
                    <span className="agent-pick-name">跟随智能体默认</span>
                    <span className="card-sub">{modelOptions.default ?? '由内核决定'}</span>
                  </span>
                  {!modelDraft && <span className="pill ok">当前</span>}
                </button>
                {modelOptions.models.map((item) => (
                  <button
                    key={item}
                    type="button"
                    className={`agent-pick${modelDraft === item ? ' active' : ''}`}
                    onClick={() => setModelDraft(item)}
                  >
                    <span className="agent-pick-main">
                      <span className="agent-pick-name mono-sm">{item}</span>
                    </span>
                    {modelDraft === item && <span className="pill ok">当前</span>}
                  </button>
                ))}
                {modelDraft && !modelOptions.models.includes(modelDraft) && (
                  <button type="button" className="agent-pick active" onClick={() => setModelDraft(modelDraft)}>
                    <span className="agent-pick-main">
                      <span className="agent-pick-name mono-sm">{modelDraft}</span>
                      <span className="card-sub">不在网关白名单里，保存后仍会按这个名字运行</span>
                    </span>
                    <span className="pill ok">当前</span>
                  </button>
                )}
              </div>
            ) : (
              <div className="stack" style={{ gap: 8 }}>
                <label className="field">
                  <span className="field-label">模型名</span>
                  <input
                    className="input mono"
                    autoFocus
                    value={modelDraft}
                    onChange={(event) => setModelDraft(event.target.value)}
                    placeholder={modelOptions.default ?? '例如 sonnet / gpt-5-codex'}
                  />
                </label>
                <p className="field-help">
                  {modelOptions.gateway
                    ? `当前网关「${modelOptions.gateway}」没有勾选模型白名单，可直接填写模型名。`
                    : '官方默认链路：可直接填写模型名，留空则使用内核默认模型。'}
                </p>
              </div>
            )}
          </div>
        </Modal>
      )}

      {actionsOpen && active && (
        <Modal
          title={`「${active.title}」的操作`}
          onClose={() => setActionsOpen(false)}
          footer={
            <button className="btn ghost" onClick={() => setActionsOpen(false)}>
              关闭
            </button>
          }
        >
          <div className="stack" style={{ gap: 8 }}>
            <button
              className="btn"
              disabled={busy || remoteRunning || messages.length === 0}
              onClick={() => {
                setActionsOpen(false)
                void send('', true)
              }}
            >
              ↻ 重新生成最后一条回复
            </button>
            <button className="btn" disabled={busy || !active.session_id} onClick={() => void resetSession()}>
              ⟲ 重置智能体记忆
            </button>
            <button className="btn" disabled={!active.workspace_dir} onClick={() => void copyWorkspace()}>
              📋 复制工作目录
            </button>
            <button className="btn danger" onClick={() => void removeConversation()}>
              删除这个对话
            </button>
          </div>
        </Modal>
      )}
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}

