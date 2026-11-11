import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../../components/MessageView'
import { api, streamGet, streamPost } from '../../lib/api'
import { collabChoiceAnswer, findPendingCollabAsks } from '../../lib/collabAsk'
import { applyChatEvent, dismissChatNotice, emptyChatTurn, type ChatTurnState } from '../../lib/collabChat'
import {
  applyCollabEvent, dismissCollabNotice, makeCollabTurn, type CollabNotice, type CollabTurnState,
} from '../../lib/collabStream'
import {
  forgetDismissedNotices, loadDismissedNotices, rememberDismissedNotice,
} from '../../lib/collabNotices'
import { buildCollabProgress } from '../../lib/collabProgress'
import type {
  CollabChoice, CollabEvent, CollabRunDetail, CollabTeam, Role, StreamClosedEvent,
} from '../../lib/types'
import { useAutoRefresh } from '../../lib/useAutoRefresh'
import { MobileCollabProgress } from '../components/MobileCollabProgress'
import { MobileRoleConversationSheet } from '../components/MobileRoleConversationSheet'
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
  const [roles, setRoles] = useState<Role[]>([])
  const [turn, setTurn] = useState<CollabTurnState | null>(null)
  //: 用户点过「×」关掉的提醒（按运行记，刷新后不再重建）
  const dismissedNotices = useMemo(() => loadDismissedNotices(runId ?? ''), [runId])
  const [chatTurn, setChatTurn] = useState<ChatTurnState>(emptyChatTurn)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [chatBusy, setChatBusy] = useState(false)
  //: 空闲时的发送模式：auto = 有待回答的问题就走工单、否则只聊天；chat/work 是用户手动指定。
  const [idleMode, setIdleMode] = useState<'auto' | 'chat' | 'work'>('auto')
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [selectedRoleKey, setSelectedRoleKey] = useState<string | null>(null)
  const [pendingUser, setPendingUser] = useState<{ id: string; text: string } | null>(null)
  const [reconnecting, setReconnecting] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const chatAbortRef = useRef<AbortController | null>(null)
  const busyRef = useRef(false)
  const chatBusyRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])
  const leaderKey = detail?.entry_key ?? team?.entry_key ?? ''
  const leaderLabel = team?.graph.nodes.find((node) => node.key === leaderKey)?.label || leaderKey || '领导者'
  const visibleSteps = useMemo(
    () => (detail?.steps ?? []).filter((step) => step.role_key === 'user' || step.role_key === leaderKey),
    [detail?.steps, leaderKey],
  )
  const chatMessages = detail?.chat_messages ?? []
  const leaderLive = turn?.live?.nodeKey === leaderKey ? turn.live : null

  const load = useCallback(async (id: string) => {
    try {
      const next = await api.getCollabRun(id)
      setDetail(next)
      setTurn((prev) => {
        const base = prev ?? makeCollabTurn(next, next.tickets, next.steps, dismissedNotices)
        return {
          ...base,
          status: next.status,
          reason: next.error ?? null,
          live: null,
          completed: [],
          tickets: Object.fromEntries(next.tickets.map((ticket) => [ticket.id, ticket])),
        }
      })
      return next
    } catch (e) {
      setError(describeError(e))
      return null
    }
  }, [dismissedNotices])

  // 协作组与角色只用于把步骤渲染成「谁的产出」：头像与岗位名。
  useEffect(() => {
    void (async () => {
      try {
        const [teamList, roleList] = await Promise.all([api.listCollabTeams(), api.listRoles()])
        setTeams(teamList)
        setRoles(roleList)
      } catch (e) {
        setError(describeError(e))
      }
    })()
  }, [])

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

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [visibleSteps.length, chatMessages.length, leaderLive?.agent.text, chatTurn.live?.agent.text])

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
      let closed = false
      const onEvent = (event: RunEvent) => {
        if (event.type === 'stream.closed') {
          closed = true
          return
        }
        setTurn((prev) => (prev ? applyCollabEvent(prev, event) : prev))
      }
      try {
        if (kickoff) await kickoff(onEvent, controller.signal)
        let attempt = 0
        while (!closed && !controller.signal.aborted && attempt < 5) {
          attempt += 1
          setReconnecting(true)
          await delay(Math.min(800 * attempt, 3000))
          if (controller.signal.aborted) break
          await streamGet<RunEvent>(`/api/collab/runs/${target}/stream`, onEvent, controller.signal)
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describeError(e))
      } finally {
        busyRef.current = false
        abortRef.current = null
        setReconnecting(false)
        setBusy(false)
        await load(target)
      }
    },
    [load],
  )

  /** 入口角色独立聊天通道：主流程在跑时也不占用、不打断这里的流。 */
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
    setSelectedRoleKey(null)
    setChatTurn(emptyChatTurn())
    setIdleMode('auto')
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
      const pending = userText ? { id: `local-user-${Date.now()}`, text: userText } : null
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

  const sendMessage = async (text: string) => {
    if (!detail || !text.trim()) return
    if (status === 'running' || busy || chatActive) {
      const pending = { id: `local-user-${Date.now()}`, text }
      setPendingUser(pending)
      setError(null)
      try {
        await subscribeChat(detail.id, (onEvent, signal) =>
          streamPost<RunEvent>(`/api/collab/runs/${detail.id}/chat`, { content: text }, onEvent, signal),
        )
      } finally {
        setPendingUser(null)
      }
      return
    }
    await stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
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
      setError(describeError(e))
      return
    }
    await stream(`/api/collab/runs/${detail?.id}/resume`, {})
  }

  const answerAsk = (choice: CollabChoice) => {
    if (!detail || busy || chatBusy) return
    const text = collabChoiceAnswer(choice)
    if (!text) {
      composerRef.current?.focus()
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
      setError(describeError(e))
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
          const role = roles.find((item) => item.id === (step.role_id ?? node?.role_id))
          return {
            id: step.id,
            avatar: role?.avatar ?? '🤖',
            label: node?.label ?? step.role_name ?? step.role_key,
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
              avatar: roles.find((role) => role.id === row.role_id)?.avatar ?? '🤖',
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
        message: { id: pendingUser.id, role: 'user', content: pendingUser.text, parts: [], status: 'complete' },
      })
    }
    const persisted = new Set(visibleSteps.map((step) => step.id))
    for (const step of turn?.completed ?? []) {
      if (step.nodeKey !== leaderKey || persisted.has(step.stepId)) continue
      const node = team?.graph.nodes.find((item) => item.key === step.nodeKey)
      const role = roles.find((item) => item.id === node?.role_id)
      entries.push({
        id: step.stepId,
        avatar: role?.avatar ?? '🤖',
        label: node?.label ?? step.roleName,
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
  }, [detail, team, roles, visibleSteps, chatMessages, pendingUser, turn?.completed, leaderKey, leaderLabel])

  const liveEntry: TimelineEntry | null = leaderLive
    ? {
        id: 'live',
        avatar: roles.find(
          (role) => role.id === team?.graph.nodes.find((node) => node.key === leaderLive.nodeKey)?.role_id,
        )?.avatar ?? '🤖',
        label: leaderLive.roleName,
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
        avatar: roles.find((role) => role.id === team?.graph.nodes.find(
          (node) => node.key === (chatTurn.live?.roleKey || leaderKey),
        )?.role_id)?.avatar ?? '🤖',
        label: team?.graph.nodes.find(
          (node) => node.key === (chatTurn.live?.roleKey || leaderKey),
        )?.label ?? chatTurn.live.roleName,
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
  const canResume = ['waiting_user', 'error', 'interrupted', 'cancelled'].includes(status)
  const remoteRunning = status === 'running' && !busy
  const progress = useMemo(
    () =>
      buildCollabProgress({
        graph: team?.graph,
        entryKey: leaderKey,
        steps: detail?.steps ?? [],
        tickets: detail?.tickets ?? [],
        roles,
        live: turn?.live ?? null,
        streamed: turn?.completed ?? [],
        runStatus: status,
      }),
    [team?.graph, leaderKey, detail?.steps, detail?.tickets, roles, turn?.live, turn?.completed, status],
  )
  const selectedRole = progress.nodes.find((node) => node.key === selectedRoleKey) ?? null
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
  //: 任务开始后、主流程空闲时，默认先聊天：问进度/补要求不会白跑一步工单。
  //: auto 模式下「有待回答的问题」自动走推进任务，避免把答复误发进聊天。
  const idle = Boolean(detail?.goal) && status !== 'running' && !busy
  const chatActive = idle && (idleMode === 'chat' || (idleMode === 'auto' && pendingAsks.length === 0))

  return (
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

      <MobileCollabProgress nodes={progress.nodes} edges={progress.edges} onOpenRole={setSelectedRoleKey} />

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
            协作正在由「{besideLeader.roleName}」处理，点上方进度可查看实时输出。
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

      <div className="m-thread" ref={scrollRef}>
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
      </div>

      <div className="m-composer">
        {(busy || remoteRunning) && (
          <div className="m-collab-running-bar">
            <span className="dim small">
              领导者团队正在推进{turn?.live ? `「${turn.live.roleName}」` : ''}；你可以继续找 CEO 细化要求或问进度
            </span>
            <button className="btn sm danger" onClick={() => void stop()}>
              {busy ? '■ 停止' : '叫停'}
            </button>
          </div>
        )}
        {idle && (
          <div className="row" style={{ gap: 8, alignItems: 'center', padding: '0 2px 7px' }}>
            <button
              type="button"
              className={`btn sm${chatActive ? ' primary' : ' ghost'}`}
              onClick={() => setIdleMode(chatActive ? 'work' : 'chat')}
            >
              {chatActive ? '💬 聊天中' : '▶ 推进任务'}
            </button>
            <span className="dim small">
              {chatActive
                ? pendingAsks.length > 0
                  ? '聊天模式：上方有待回答的问题，回答请点选项'
                  : '只对话，不推进工单'
                : '发送后会继续推进工单'}
            </span>
          </div>
        )}
        <div className="m-composer-box">
          <textarea
            ref={composerRef}
            rows={1}
            value={draft}
            disabled={chatBusy}
            placeholder={
              chatBusy
                ? 'CEO 正在回复…'
                : chatActive
                  ? '和 CEO 聊聊：问进度、补要求、随便说两句…'
                : status === 'running'
                  ? '随时问 CEO：细化要求、问进度、聊两句…'
                  : detail?.goal
                    ? '推进任务：回答提问，或让 CEO 继续推进工单…'
                    : `和领导者「${leaderLabel}」对话…`
            }
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                if (!chatBusy && draft.trim()) {
                  const text = draft
                  setDraft('')
                  void sendMessage(text)
                }
              }
            }}
          />
          <button
            className="btn primary"
            disabled={chatBusy || !draft.trim()}
            onClick={() => {
              const text = draft
              setDraft('')
              void sendMessage(text)
            }}
          >
            {chatBusy ? '回复中…' : '发送'}
          </button>
        </div>
        {canResume && !busy && !chatBusy && (
          <button
            className="btn ghost block m-composer-secondary"
            onClick={() => void stream(`/api/collab/runs/${detail?.id}/resume`, {})}
          >
            让领导者继续推进（不补充内容）
          </button>
        )}
      </div>

      {selectedRole && detail && (
        <MobileRoleConversationSheet
          node={selectedRole}
          roster={progress.nodes}
          steps={detail.steps}
          tickets={detail.tickets}
          live={turn?.live ?? null}
          streamed={turn?.completed ?? []}
          onClose={() => setSelectedRoleKey(null)}
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
  )
}
