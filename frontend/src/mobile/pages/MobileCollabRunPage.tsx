import { useCallback, useEffect, useMemo, useRef, useState, type Ref } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../../components/MessageView'
import { api, collabRunFileUrl, isNetworkError, streamGet, streamPost } from '../../lib/api'
import { useComposerDraft } from '../../lib/composerDraft'
import { collabChoiceAnswer, findPendingCollabAsks } from '../../lib/collabAsk'
import { applyChatEvent, dismissChatNotice, emptyChatTurn, type ChatTurnState } from '../../lib/collabChat'
import { MediaUrlProvider, composeWithAttachments } from '../../lib/media'
import {
  applyCollabEvent, dismissCollabNotice, syncTurnWithRun, type CollabNotice, type CollabTurnState,
} from '../../lib/collabStream'
import {
  forgetDismissedNotices, loadDismissedNotices, rememberDismissedNotice,
} from '../../lib/collabNotices'
import { buildCollabProgress } from '../../lib/collabProgress'
import type {
  AttachmentRef, CollabChoice, CollabEvent, CollabRunDetail, CollabTeam, AgentProfile, StreamClosedEvent,
} from '../../lib/types'
import { useAutoRefresh } from '../../lib/useAutoRefresh'
import { MobileCollabProgress } from '../components/MobileCollabProgress'
import { Composer } from '../../components/Composer'
import { MobileAgentConversationSheet } from '../components/MobileAgentConversationSheet'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const STATUS_LABEL: Record<string, string> = {
  running: '进行中',
  waiting_user: '等待你决定',
  done: '已完成',
  error: '出错',
  cancelled: '已取消',
  interrupted: '已中断',
}

const STATUS_PILL: Record<string, string> = {
  running: 'ok',
  waiting_user: 'warn',
  done: 'brand',
  error: 'err',
  cancelled: '',
  interrupted: 'warn',
}

interface TimelineEntry {
  id: string
  message: RenderMessage
  avatar?: string
  label: string
}

/** 协作运行详情：步骤时间线 + 预算进度，可叫停、继续、回复入口岗位。 */
/** 一次订阅可能收到协作信封，也可能收到传输层的 ``stream.closed`` 收尾标记。 */
type RunEvent = CollabEvent | StreamClosedEvent

/** 重连前的等待：每次退避一点，避免断线时疯狂重试。 */
const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

export function MobileCollabRunPage() {
  const { runId } = useParams<{ runId: string }>()
  const navigate = useNavigate()

  const [detail, setDetail] = useState<CollabRunDetail | null>(null)
  const [teams, setTeams] = useState<CollabTeam[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [turn, setTurn] = useState<CollabTurnState | null>(null)
  //: 用户点过「×」关掉的提醒（按运行记，刷新后不再重建）
  const dismissedNotices = useMemo(() => loadDismissedNotices(runId ?? ''), [runId])
  const [chatTurn, setChatTurn] = useState<ChatTurnState>(emptyChatTurn)
  /**
   * 草稿（文字 + 待发附件）按「运行」各记各的：切到别的运行再切回来，没发出去的内容还在原处。
   */
  const { text: draft, setText: setDraft, attachments, setAttachments } = useComposerDraft(runId)
  const [busy, setBusy] = useState(false)
  const [chatBusy, setChatBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(null)
  /** 与入口智能体的对话界面：默认不显示，点动画里的人物才弹出来 */
  const [chatOpen, setChatOpen] = useState(false)
  /** 发送模式：对话（只和入口智能体聊）还是推进任务（把这句话接到工单上接着跑） */
  const [sendMode, setSendMode] = useState<'chat' | 'work'>('chat')
  const [pendingUser, setPendingUser] = useState<{ id: string; text: string; attachments: string[] } | null>(null)
  const [reconnecting, setReconnecting] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const chatAbortRef = useRef<AbortController | null>(null)
  const busyRef = useRef(false)
  const chatBusyRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const sheetComposerRef = useRef<HTMLTextAreaElement | null>(null)
  /** 当前横幅上的错误是不是「网络中断」（可自愈）：下一次拉取成功就把它撤掉，别让它一直挂着。 */
  const networkErrorRef = useRef(false)

  /** 统一上报异常：记下这次是不是网络中断，好让恢复后的第一次成功拉取把横幅撤掉。 */
  const reportError = useCallback((e: unknown) => {
    networkErrorRef.current = isNetworkError(e)
    setError(describeError(e))
  }, [])

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])
  const leaderKey = detail?.entry_key ?? team?.entry_key ?? ''
  const leaderLabel = team?.graph.nodes.find((node) => node.key === leaderKey)?.label || leaderKey || '领导者'
  // 同一步骤既有落库行（可能还停在 streaming、内容为空）又有流式结果时，以流式结果为准。
  const streamedStepIds = useMemo(
    () => new Set([
      ...(turn?.completed ?? []).map((step) => step.stepId),
      ...(turn?.live ? [turn.live.stepId] : []),
    ]),
    [turn?.completed, turn?.live],
  )
  const visibleSteps = useMemo(
    () => (detail?.steps ?? []).filter(
      (step) => (step.role_key === 'user' || step.role_key === leaderKey) && !streamedStepIds.has(step.id),
    ),
    [detail?.steps, leaderKey, streamedStepIds],
  )
  const chatMessages = detail?.chat_messages ?? []
  const leaderLive = turn?.live?.nodeKey === leaderKey ? turn.live : null

  const load = useCallback(async (id: string) => {
    try {
      const next = await api.getCollabRun(id)
      setDetail(next)
      setTurn((prev) => syncTurnWithRun(prev, next, next.tickets, next.steps, dismissedNotices))
      // 连接恢复（切回 PWA、重连成功）：把上一次「网络中断」的横幅撤掉，别让它长眠在界面上。
      if (networkErrorRef.current) {
        networkErrorRef.current = false
        setError(null)
      }
      return next
    } catch (e) {
      reportError(e)
      return null
    }
  }, [dismissedNotices, reportError])

  // 协作组与智能体只用于把步骤渲染成「谁的产出」：头像与岗位名。
  useEffect(() => {
    void (async () => {
      try {
        const [teamList, agentList] = await Promise.all([api.listCollabTeams(), api.listAgents()])
        setTeams(teamList)
        setAgents(agentList)
      } catch (e) {
        reportError(e)
      }
    })()
  }, [reportError])

  // 离开页面时退订；运行本身在服务端继续，回来再订阅即可。
  useEffect(() => () => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
  }, [])

  // SSE 只负责本设备发起的增量；运行详情以落库结果为准，并定时对齐其他设备。
  useAutoRefresh(() => (runId ? load(runId) : undefined), {
    intervalMs: 4000,
    enabled: Boolean(runId) && !busy,
  })

  // 内容变化（新步骤、流式追加）或对话弹层刚打开时，把时间线滚到底部。
  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [visibleSteps.length, chatMessages.length, leaderLive?.agent.text, chatTurn.live?.agent.text, chatOpen])

  /**
   * 订阅一次运行的实时事件并渲染。
   *
   * 推进跑在服务端独立任务里：连接被切断（手机锁屏、切后台、换网络）时这里会自动
   * 重连订阅续看，而不是把运行打断；`kickoff` 用于「发消息 / 继续」这类先启动再订阅的场景。
   */
  const subscribe = useCallback(
    async (
      target: string,
      kickoff?: (onEvent: (event: RunEvent) => void, signal: AbortSignal) => Promise<void>,
    ) => {
      if (busyRef.current) return
      busyRef.current = true
      setBusy(true)
      const controller = new AbortController()
      abortRef.current = controller
      const onEvent = (event: RunEvent) => {
        if (event.type === 'stream.closed') return
        setTurn((prev) => (prev ? applyCollabEvent(prev, event) : prev))
      }
      try {
        if (kickoff) {
          try {
            await kickoff(onEvent, controller.signal)
          } catch (e) {
            // 发起这一轮的响应断了不代表运行停了：先看落库状态，还在跑就继续订阅围观。
            if ((e as Error)?.name === 'AbortError') throw e
            reportError(e)
          }
          if (controller.signal.aborted) return
          const latest = await load(target)
          if (!latest || latest.status !== 'running') return
        }
        let attempt = 0
        while (!controller.signal.aborted && attempt < 30) {
          if (attempt > 0) {
            setReconnecting(true)
            await delay(Math.min(800 * attempt, 3000))
            if (controller.signal.aborted) break
          }
          attempt += 1
          try {
            await streamGet<RunEvent>(`/api/collab/runs/${target}/stream`, onEvent, controller.signal)
          } catch (e) {
            // 网络层断开不是运行结束：退避后重订阅，总线会补发本轮事件。
            if ((e as Error)?.name === 'AbortError') throw e
            reportError(e)
          }
          if (controller.signal.aborted) break
          // 以落库状态为准：运行真的收尾了才停，否则继续订阅。
          const latest = await load(target)
          if (!latest || latest.status !== 'running') break
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') reportError(e)
      } finally {
        busyRef.current = false
        abortRef.current = null
        setReconnecting(false)
        setBusy(false)
        await load(target)
      }
    },
    [load, reportError],
  )

  /** 入口智能体独立聊天通道：主流程在跑时也不占用、不打断这里的流。 */
  const subscribeChat = useCallback(
    async (
      target: string,
      kickoff?: (onEvent: (event: RunEvent) => void, signal: AbortSignal) => Promise<void>,
    ) => {
      if (chatBusyRef.current) return
      chatBusyRef.current = true
      setChatBusy(true)
      const controller = new AbortController()
      chatAbortRef.current = controller
      let closed = false
      const onEvent = (event: RunEvent) => {
        if (event.type === 'stream.closed') {
          closed = true
          return
        }
        if (
          event.type === 'collab.chat.started'
          || event.type === 'collab.chat.event'
          || event.type === 'collab.chat.finished'
        ) {
          setChatTurn((prev) => applyChatEvent(prev, event))
        }
      }
      try {
        if (kickoff) await kickoff(onEvent, controller.signal)
        if (!closed && !controller.signal.aborted && !kickoff) {
          await streamGet<RunEvent>(`/api/collab/runs/${target}/chat/stream`, onEvent, controller.signal)
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describeError(e))
      } finally {
        chatBusyRef.current = false
        chatAbortRef.current = null
        setChatBusy(false)
        await load(target)
      }
    },
    [load],
  )

  useEffect(() => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    if (!runId) return
    setError(null)
    setSelectedNodeKey(null)
    setChatOpen(false)
    setAttachments([])
    setSendMode('chat')
    setChatTurn(emptyChatTurn())
    void (async () => {
      const next = await load(runId)
      // 这次运行在跑就订阅实时事件：本页发起的、或别的设备/上次会话发起的都一样。
      if (next && next.status === 'running' && !busyRef.current) void subscribe(runId)
      if (next?.chat_active && !chatBusyRef.current) void subscribeChat(runId)
    })()
  }, [runId, load, subscribe, subscribeChat])

  const stream = useCallback(
    async (path: string, body: unknown, userText?: string) => {
      if (!detail) return
      const pending = userText
        ? { id: `local-user-${Date.now()}`, text: userText, attachments: [] }
        : null
      if (pending) setPendingUser(pending)
      setError(null)
      try {
        await subscribe(detail.id, (onEvent, signal) => streamPost<RunEvent>(path, body, onEvent, signal))
      } finally {
        if (pending) setPendingUser(null)
      }
    },
    [detail, subscribe],
  )

  /**
   * 发一条消息，走哪条线由输入区的「对话 / 推进任务」决定（见 `effectiveWork`）。
   *
   * - **对话**：交给入口智能体的聊天通道；带附件时一定走这条 —— 附件只落在聊天这条线上
   *   （工单推进的输入是结构化工单，没有附件位），而入口智能体本来就能把新要求同步进工单；
   * - **推进任务**：把这句话接到工单上接着跑（`/messages`）；正文为空就是让领导者继续推进
   *   （`/resume`，不追加用户消息）。
   */
  const sendMessage = async (
    text: string,
    files: AttachmentRef[] = [],
    mode: 'chat' | 'work' = 'chat',
  ) => {
    if (!detail) return
    if (!text.trim() && files.length === 0) return
    if (mode !== 'work' || files.length > 0) {
      const paths = files.map((item) => item.rel_path)
      // 直接进对话界面：发出的内容与回复都要看得见。
      setChatOpen(true)
      const pending = {
        id: `local-user-${Date.now()}`,
        text: text.trim(),
        attachments: paths,
      }
      setPendingUser(pending)
      setError(null)
      /** 第一个事件到达即代表消息已落库：此刻把待发附件从输入区收走，而不是等整轮聊天跑完。 */
      let cleared = paths.length === 0
      try {
        await subscribeChat(detail.id, (onEvent, signal) =>
          streamPost<RunEvent>(
            `/api/collab/runs/${detail.id}/chat`,
            { content: text, attachments: paths },
            (event) => {
              if (!cleared) {
                cleared = true
                setAttachments([])
              }
              onEvent(event)
            },
            signal,
          ),
        )
      } finally {
        setPendingUser(null)
      }
      return
    }
    // 推进任务：不补充内容就是「让领导者继续推进」，走 resume。
    if (!text.trim()) {
      await stream(`/api/collab/runs/${detail.id}/resume`, {})
      return
    }
    await stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
  }

  /** 输入区（共享组件）把读好的 base64 交回来，这里只负责调接口。 */
  const uploadAttachment = async (name: string, contentBase64: string): Promise<AttachmentRef> => {
    if (!detail) throw new Error('这次协作还没加载完，稍后再试')
    return api.uploadCollabAttachment(detail.id, { name, content_base64: contentBase64 })
  }

  /** 输入区发出来的内容：按当前模式交给 `sendMessage`，附件由它决定何时清空。 */
  const submitDraft = () => {
    const text = draft
    setDraft('')
    void sendMessage(text, attachments, effectiveWork ? 'work' : 'chat')
  }

  /** 点动画里的人物：入口智能体打开对话界面（它的对话就是与用户的这条线），其他岗位看只读会话。 */
  const openNode = (key: string) => {
    if (key === leaderKey) {
      setChatOpen(true)
      return
    }
    setSelectedNodeKey(key)
  }

  /** 关掉一条提醒：立即移除，并记住「用户已经看过」，刷新后不再冒出来。 */
  const dismissNotice = (notice: CollabNotice) => {
    setTurn((prev) => (prev ? dismissCollabNotice(prev, notice.key) : prev))
    rememberDismissedNotice(runId ?? detail?.id ?? '', notice.key)
  }

  const resumeWithRetry = async (stepId: string) => {
    try {
      await api.retryCollabStep(stepId)
    } catch (e) {
      reportError(e)
      return
    }
    await stream(`/api/collab/runs/${detail?.id}/resume`, {})
  }

  const answerAsk = (choice: CollabChoice) => {
    if (!detail || busy || chatBusy) return
    const text = collabChoiceAnswer(choice)
    if (!text) {
      sheetComposerRef.current?.focus()
      return
    }
    setDraft('')
    void stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
  }

  const stop = async () => {
    if (!detail) return
    setMenuOpen(false)
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    // 尽力而为：本地流已经断开，服务端叫停失败不影响用户（引擎会按预算自行收尾）。
    await api.cancelCollabRun(detail.id).catch(() => undefined)
    await load(detail.id)
  }

  const remove = async () => {
    if (!detail) return
    setMenuOpen(false)
    if (!confirm(`删除运行「${detail.title}」及其全部工单与步骤？`)) return
    try {
      await api.deleteCollabRun(detail.id)
    } catch (e) {
      reportError(e)
      return
    }
    forgetDismissedNotices(detail.id)
    navigate('/collab', { replace: true })
  }

  const timeline = useMemo<TimelineEntry[]>(() => {
    if (!detail) return []
    const pairs = [
      ...visibleSteps.map((step) => ({
        at: step.started_at,
        entry: (() => {
          if (step.role_key === 'user') {
            return {
              id: step.id,
              label: '你',
              message: { id: step.id, role: 'user' as const, content: step.content, parts: [], status: 'complete' },
            }
          }
          const node = team?.graph.nodes.find((item) => item.key === step.role_key)
          const agent = agents.find((item) => item.id === (step.agent_profile_id ?? node?.agent_profile_id))
          return {
            id: step.id,
            avatar: agent?.avatar ?? '🤖',
            label: node?.label ?? step.profile_name ?? step.role_key,
            message: {
              id: step.id,
              role: 'assistant' as const,
              content: step.content,
              parts: step.parts,
              status: step.status,
              error: step.error,
              usage: step.usage,
            },
          }
        })(),
      })),
      ...chatMessages.map((row) => ({
        at: row.created_at,
        entry: row.sender === 'user'
          ? {
              id: row.id,
              label: '你',
              message: { id: row.id, role: 'user' as const, content: row.content, parts: [], status: 'complete' },
            }
          : {
              id: row.id,
              avatar: agents.find((agent) => agent.id === row.agent_profile_id)?.avatar ?? '🤖',
              label: leaderLabel,
              message: {
                id: row.id,
                role: 'assistant' as const,
                content: row.content,
                parts: row.parts,
                status: row.status,
                error: row.error,
                usage: row.usage,
              },
            },
      })),
    ]
    const entries: TimelineEntry[] = pairs
      .sort((left, right) => String(left.at).localeCompare(String(right.at)))
      .map((pair) => pair.entry)
    if (pendingUser) {
      entries.push({
        id: pendingUser.id,
        label: '你',
        message: {
          id: pendingUser.id,
          role: 'user',
          // 附件清单拼回正文：图片缩略图当场就画进「你」的气泡，不用等落库后重新拉取。
          content: composeWithAttachments(pendingUser.text, pendingUser.attachments),
          parts: [],
          status: 'complete',
        },
      })
    }
    const persisted = new Set(visibleSteps.map((step) => step.id))
    for (const step of turn?.completed ?? []) {
      if (step.nodeKey !== leaderKey || persisted.has(step.stepId)) continue
      const node = team?.graph.nodes.find((item) => item.key === step.nodeKey)
      const agent = agents.find((item) => item.id === node?.agent_profile_id)
      entries.push({
        id: step.stepId,
        avatar: agent?.avatar ?? '🤖',
        label: node?.label ?? step.agentName,
        message: {
          id: step.stepId,
          role: 'assistant',
          content: step.agent.text,
          parts: step.agent.parts,
          status: step.error ? 'error' : step.status,
          error: step.error ?? step.agent.error,
          usage: step.agent.usage,
          logs: step.agent.logs,
        },
      })
    }
    return entries
  }, [detail, team, agents, visibleSteps, chatMessages, pendingUser, turn?.completed, leaderKey, leaderLabel])

  const liveEntry: TimelineEntry | null = leaderLive
    ? {
        id: 'live',
        avatar: agents.find(
          (agent) => agent.id === team?.graph.nodes.find((node) => node.key === leaderLive.nodeKey)?.agent_profile_id,
        )?.avatar ?? '🤖',
        label: leaderLive.agentName,
        message: {
          id: 'live',
          role: 'assistant',
          content: leaderLive.agent.text,
          parts: leaderLive.agent.parts,
          status: leaderLive.agent.error ? 'error' : 'streaming',
          error: leaderLive.agent.error,
          usage: leaderLive.agent.usage,
          streaming: leaderLive.agent.status === 'running',
          logs: leaderLive.agent.logs,
        },
      }
    : null

  const chatLiveEntry: TimelineEntry | null = chatTurn.live
    ? {
        id: `chat-live-${chatTurn.live.messageId}`,
        avatar: agents.find((agent) => agent.id === team?.graph.nodes.find(
          (node) => node.key === (chatTurn.live?.roleKey || leaderKey),
        )?.agent_profile_id)?.avatar ?? '🤖',
        label: team?.graph.nodes.find(
          (node) => node.key === (chatTurn.live?.roleKey || leaderKey),
        )?.label ?? chatTurn.live.agentName,
        message: {
          id: `chat-live-${chatTurn.live.messageId}`,
          role: 'assistant',
          content: chatTurn.live.agent.text,
          parts: chatTurn.live.agent.parts,
          status: chatTurn.live.agent.error ? 'error' : 'streaming',
          error: chatTurn.live.agent.error,
          usage: chatTurn.live.agent.usage,
          streaming: chatTurn.live.agent.status === 'running',
          logs: chatTurn.live.agent.logs,
        },
      }
    : null

  const status = turn?.status ?? detail?.status ?? 'running'
  // 详情轮询发现运行又推进起来了（另一台设备点了继续 / 本页此前掉线），而这里已经退订：
  // 自动补一次订阅，办公室里的岗位实时对话才能继续拿到逐字输出。
  useEffect(() => {
    if (!runId || status !== 'running' || busyRef.current) return
    const timer = window.setTimeout(() => {
      if (!busyRef.current) void subscribe(runId)
    }, 800)
    return () => window.clearTimeout(timer)
  }, [runId, status, subscribe])
  const remoteRunning = status === 'running' && !busy
  const progress = useMemo(
    () =>
      buildCollabProgress({
        graph: team?.graph,
        entryKey: leaderKey,
        steps: detail?.steps ?? [],
        tickets: detail?.tickets ?? [],
        agents,
        live: turn?.live ?? null,
        streamed: turn?.completed ?? [],
        runStatus: status,
      }),
    [team?.graph, leaderKey, detail?.steps, detail?.tickets, agents, turn?.live, turn?.completed, status],
  )
  const selectedNode = progress.nodes.find((node) => node.key === selectedNodeKey) ?? null
  const besideLeader = turn?.live && turn.live.nodeKey !== leaderKey ? turn.live : null
  const pendingAsks = useMemo(
    () =>
      status === 'waiting_user'
        ? findPendingCollabAsks(
            Object.values(turn?.tickets ?? {}).filter((ticket) => !detail || ticket.run_id === detail.id),
          )
        : [],
    [detail, status, turn?.tickets],
  )
  /**
   * 协作有两条线，走哪条由输入区的「对话 / 推进任务」决定：
   *
   * - **对话**：只和入口智能体聊，不推进工单（问进度、补要求），新要求会在下一轮步骤生效；
   * - **推进任务**：把这句话接到工单上接着跑。
   *
   * 有两处没法自由选，界面会直接把选择权接管过去（对应的那一档同时禁用）：
   * 任务还没开始（第一句就是任务描述，只能走推进）、工单正在推进（推进接口会 409，只能问进度）、
   * 带着附件（工单通道没有附件位，只能随对话发）、有待回答的问题（答复必须走推进，否则会被当闲聊）。
   */
  const taskStarted = Boolean(detail?.goal)
  const advancing = status === 'running' || busy
  const modeToggleVisible = taskStarted && !advancing
  const modeLockedByAttachments = attachments.length > 0
  const modeLockedByAsk = modeToggleVisible && pendingAsks.length > 0
  const effectiveWork =
    !advancing && !modeLockedByAttachments && (!taskStarted || modeLockedByAsk || sendMode === 'work')
  const showComposerHeader = modeToggleVisible || pendingAsks.length > 0 || busy || remoteRunning

  /**
   * 输入框上方的那一条：左侧一个按钮切换发送模式（点一下「推进任务」，再点回「普通对话」），
   * 右侧一句当前模式的说明；需要拍板 / 正在推进这类状态挂在下面。平时不硬塞东西。
   */
  const modeHint = modeLockedByAttachments
    ? '附件只能随普通对话发送'
    : modeLockedByAsk
      ? '有待回答的问题：发送后继续推进'
      : effectiveWork
        ? draft.trim()
          ? '发送后接着跑工单'
          : '发送即让领导者继续推进'
        : '不推进工单，只和入口智能体聊'
  const composerHeader = showComposerHeader ? (
    // 外层容器由共享的 Composer 渲染（`.composer-head`），这里只放内容，别再套一层。
    <>
      {modeToggleVisible && (
        <div className="m-mode-row">
          <button
            type="button"
            className={`m-mode-toggle${effectiveWork ? ' on' : ''}`}
            aria-pressed={effectiveWork}
            disabled={modeLockedByAsk || modeLockedByAttachments}
            onClick={() => setSendMode(effectiveWork ? 'chat' : 'work')}
            title={
              modeLockedByAttachments
                ? '附件只能随普通对话发送；去掉附件后可以切到推进任务'
                : modeLockedByAsk
                  ? '有待回答的问题，发送后会继续推进'
                  : effectiveWork
                    ? '点一下切回普通对话（不推进工单）'
                    : '点一下切到推进任务（发送后接着跑工单；空着发就是继续推进）'
            }
          >
            {effectiveWork ? '推进任务' : '普通对话'}
          </button>
          <span className="m-mode-hint dim small">{modeHint}</span>
        </div>
      )}
      {pendingAsks.length > 0 && (
        <button className="m-run-ask" onClick={() => setChatOpen(true)}>
          <span className="pill warn">需要你决定</span>
          <span className="m-run-ask-text">{pendingAsks[0].question}</span>
          <span className="m-run-ask-go">去回答 ›</span>
        </button>
      )}
      {(busy || remoteRunning) && (
        <div className="m-collab-running-bar">
          <span className="dim small">
            领导者团队正在推进{turn?.live ? `「${turn.live.agentName}」` : ''}；点办公室里的领导者可以问进度
          </span>
          <button className="btn sm danger" onClick={() => void stop()}>
            {busy ? '■ 停止' : '叫停'}
          </button>
        </div>
      )}
    </>
  ) : undefined

  /** 输入区（共享组件）：主页面与对话弹层共用这一个渲染函数，ref 分别绑定当前可见的输入框。 */
  const renderComposer = (inputRef?: Ref<HTMLTextAreaElement>) => (
    <Composer
      value={draft}
      onChange={setDraft}
      onSend={submitDraft}
      attachments={attachments}
      onAttachmentsChange={setAttachments}
      /* 任务还没开始时先不显示回形针：第一句是任务描述，那条线只认正文 */
      onUpload={taskStarted ? uploadAttachment : undefined}
      onStop={() => void stop()}
      busy={busy || chatBusy}
      disabled={!detail}
      submitOnEnter
      /* 推进任务模式下空着也能发：那就是「让领导者继续推进」（等价于 /resume） */
      allowEmptySend={effectiveWork}
      onNotice={(text) => {
        // 输入区的自述性提示（语音识别失败之类）：不是可自愈的网络中断，别被下一次刷新顺手撤掉。
        networkErrorRef.current = false
        setError(text)
      }}
      inputLabel={`对「${leaderLabel}」说点什么`}
      inputRef={inputRef}
      header={composerHeader}
    />
  )

  /** 对话时间线：入口智能体的产出、你与它的对话、以及需要你拍板的问题。 */
  const thread = (
    <>
      {timeline.length === 0 && !liveEntry && !chatLiveEntry && pendingAsks.length === 0 && !busy && (
        <div className="m-empty">
          <div className="m-empty-icon">🤝</div>
          <p>和领导者开始对话</p>
          <p className="dim small">先告诉领导者这次协作要完成什么。</p>
        </div>
      )}
      {timeline.map((entry) => (
        <MessageView key={entry.id} message={entry.message} avatar={entry.avatar} label={entry.label} />
      ))}
      {liveEntry && <MessageView message={liveEntry.message} avatar={liveEntry.avatar} label={liveEntry.label} />}
      {chatLiveEntry && (
        <MessageView message={chatLiveEntry.message} avatar={chatLiveEntry.avatar} label={chatLiveEntry.label} />
      )}
      {pendingAsks.map((ask) => (
        <div key={ask.ticketId} className="m-ask-card">
          <div className="row" style={{ gap: 7, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="pill warn">需要你决定</span>
            <span className="dim small">点一项作为答复，或直接在下方输入</span>
          </div>
          <div className="m-ask-question">{ask.question}</div>
          <div className="m-ask-options">
            {ask.options.map((choice, index) => (
              <button
                key={`${ask.ticketId}-${index}-${choice.label}`}
                className={`m-ask-option${choice.freeform ? ' freeform' : ''}`}
                disabled={busy || chatBusy}
                onClick={() => answerAsk(choice)}
              >
                <span>{choice.label}</span>
                {choice.description && <span className="dim small">{choice.description}</span>}
              </button>
            ))}
          </div>
        </div>
      ))}
    </>
  )

  return (
    <MediaUrlProvider resolver={(relPath) => (runId ? collabRunFileUrl(runId, relPath) : null)}>
      <div className="m-page">
      <MobileTopBar
        onBack={() => navigate('/collab')}
        title={detail?.title ?? '协作运行'}
        subtitle={
          detail
            ? `${detail.team_name ?? team?.name ?? '协作组'} · ${STATUS_LABEL[status] ?? status}`
            : '加载中…'
        }
        actions={
          <button className="m-icon-btn" onClick={() => setMenuOpen(true)} aria-label="更多操作">
            ⋯
          </button>
        }
      />

      {detail && (
        <div className="row wrap" style={{ gap: 6, padding: '10px 12px 0' }}>
          <span className={`pill ${STATUS_PILL[status] ?? ''}`}>{STATUS_LABEL[status] ?? status}</span>
          <span className="pill">
            步骤 {detail.steps_used}/{detail.budget.max_steps}
          </span>
          <span className="pill">
            工单 {detail.tickets_used}/{detail.budget.max_tickets}
          </span>
          {detail.tickets_used > 0 && <span className="pill">未结 {detail.open_tickets}</span>}
          {detail.tickets.length > 0 && (
            <button
              type="button"
              className="pill m-board-link"
              onClick={() => navigate(`/collab/runs/${detail.id}/tickets`)}
            >
              📋 工单看板 {detail.tickets.length}
            </button>
          )}
        </div>
      )}

      {/* 主页面就是那间可缩放 / 拖动的 3D 办公室；聊天界面点人物才出来。 */}
      <div className="m-run-stage">
        <MobileCollabProgress nodes={progress.nodes} edges={progress.edges} onOpenNode={openNode} />
      </div>

      {reconnecting && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="banner info">连接断了，正在自动重连订阅；协作仍在服务端继续推进。</div>
        </div>
      )}

      {error && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="banner err">{error}</div>
        </div>
      )}

      {turn && turn.notices.filter((notice) => notice.level !== 'ask').length > 0 && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="stack" style={{ gap: 6 }}>
            {turn.notices.filter((notice) => notice.level !== 'ask').slice(-3).map((notice) => (
              <div className={`banner${notice.level === 'error' ? ' err' : ''}`} key={notice.key}>
                <div className="row" style={{ gap: 8 }}>
                  <span style={{ flex: 1 }}>{notice.text}</span>
                  {notice.level === 'error' && notice.stepId && (
                    <button
                      className="btn sm"
                      disabled={busy}
                      onClick={() => void resumeWithRetry(notice.stepId as string)}
                    >
                      重试
                    </button>
                  )}
                  <button
                    className="banner-close"
                    aria-label="关掉这条提醒"
                    onClick={() => dismissNotice(notice)}
                  >
                    ✕
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {besideLeader && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="banner info">
            协作正在由「{besideLeader.agentName}」处理，点上方进度可查看实时输出。
          </div>
        </div>
      )}

      {chatTurn.notice && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className={`banner${chatTurn.notice.level === 'error' ? ' err' : chatTurn.notice.level === 'warn' ? ' warn' : ' info'}`}>
            <div className="row" style={{ gap: 8 }}>
              <span style={{ flex: 1 }}>{chatTurn.notice.text}</span>
              <button
                className="banner-close"
                aria-label="关掉这条提示"
                onClick={() => setChatTurn((prev) => dismissChatNotice(prev))}
              >
                ✕
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 底部只有输入框：附件 / 语音 / 发送都长在框里（与对话页共用 Composer）。 */}
      {renderComposer()}

      {chatOpen && detail && (
        <MobileSheet
          full
          title={`与「${leaderLabel}」的对话`}
          onClose={() => setChatOpen(false)}
          bodyClassName="m-run-chat-body"
        >
          <div className="m-thread m-run-chat-thread" ref={scrollRef}>
            {thread}
          </div>
          {renderComposer(sheetComposerRef)}
        </MobileSheet>
      )}

      {selectedNode && detail && (
        <MobileAgentConversationSheet
          node={selectedNode}
          roster={progress.nodes}
          steps={detail.steps}
          tickets={detail.tickets}
          live={turn?.live ?? null}
          streamed={turn?.completed ?? []}
          onOpenTicket={(ticketId) => navigate(`/collab/runs/${detail.id}/tickets?ticket=${ticketId}`)}
          onClose={() => setSelectedNodeKey(null)}
        />
      )}

      {menuOpen && detail && (
        <MobileSheet title="运行操作" onClose={() => setMenuOpen(false)}>
          <div className="stack" style={{ gap: 8 }}>
            <button
              className="btn block"
              onClick={() => {
                setMenuOpen(false)
                navigate(`/collab/runs/${detail.id}/tickets`)
              }}
            >
              📋 工单看板（{detail.tickets.length} 张）
            </button>
            {status === 'running' && (
              <button className="btn block danger" onClick={() => void stop()}>
                ■ 叫停这次运行
              </button>
            )}
            <button
              className="btn block"
              disabled={busy}
              onClick={() => {
                setMenuOpen(false)
                void load(detail.id)
              }}
            >
              ↻ 刷新
            </button>
            <button className="btn block danger" onClick={() => void remove()}>
              删除这次运行
            </button>
          </div>
        </MobileSheet>
      )}
      </div>
    </MediaUrlProvider>
  )
}
