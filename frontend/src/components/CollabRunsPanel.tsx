/**
 * 「协作运行」页签：左栏是运行列表，主区是时间线（复用 `MessageView`）与工单状态摘要，
 * 底部输入框直接和入口智能体说话（走协作 SSE）。
 *
 * 工单的细节（列表、流转时间线、规则与越权审计）在点击后打开的全屏 `TicketBoard`（三栏看板）里。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CollabProgressBoard } from './CollabProgressBoard'
import { ConversationThread, type ConversationThreadItem } from './ConversationThread'
import type { RenderMessage } from './MessageView'
import { TicketBoard } from './TicketBoard'
import { Modal } from './Modal'
import { api, ApiError, streamGet, streamPost } from '../lib/api'
import { ticketStateMeta } from '../lib/collabDraft'
import { collabChoiceAnswer, findPendingCollabAsks } from '../lib/collabAsk'
import { applyChatEvent, dismissChatNotice, emptyChatTurn, type ChatTurnState } from '../lib/collabChat'
import {
  applyCollabEvent, dismissCollabNotice, makeCollabTurn, type CollabNotice, type CollabTurnState,
} from '../lib/collabStream'
import {
  forgetDismissedNotices, loadDismissedNotices, rememberDismissedNotice,
} from '../lib/collabNotices'
import { buildCollabProgress } from '../lib/collabProgress'
import { useAutoRefresh } from '../lib/useAutoRefresh'
import { summarizeUsage } from '../lib/usage'
import type {
  CollabChoice, CollabEvent, CollabRun, CollabRunDetail, CollabTeam, AgentProfile, StreamClosedEvent,
} from '../lib/types'

/** 一次订阅可能收到协作信封，也可能收到传输层的 ``stream.closed`` 收尾标记。 */
type RunEvent = CollabEvent | StreamClosedEvent

/** 重连前的等待：每次退避一点，避免断线时疯狂重试。 */
const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))

const RUN_STATUS: Record<string, { label: string; pill: string }> = {
  running: { label: '进行中', pill: 'brand' },
  waiting_user: { label: '等待你', pill: 'warn' },
  done: { label: '已完成', pill: 'ok' },
  error: { label: '出错', pill: 'err' },
  cancelled: { label: '已停止', pill: '' },
  interrupted: { label: '被中断', pill: 'warn' },
}

interface Props {
  runId?: string
  teams: CollabTeam[]
  agents: AgentProfile[]
  onOpenRun: (runId: string | null) => void
}

export function CollabRunsPanel({ runId, teams, agents, onOpenRun }: Props) {
  const [runs, setRuns] = useState<CollabRun[]>([])
  const [detail, setDetail] = useState<CollabRunDetail | null>(null)
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
  const [picking, setPicking] = useState(false)
  const [startTeamId, setStartTeamId] = useState('')
  const [startTitle, setStartTitle] = useState('')
  const [startWorkspace, setStartWorkspace] = useState('')
  const [boardOpen, setBoardOpen] = useState(false)
  const [progressOpen, setProgressOpen] = useState(false)
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
  const visibleSteps = useMemo(
    () => (detail?.steps ?? []).filter((step) => step.role_key === 'user' || step.role_key === leaderKey),
    [detail?.steps, leaderKey],
  )
  const chatMessages = detail?.chat_messages ?? []
  const leaderLive = turn?.live?.nodeKey === leaderKey ? turn.live : null

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await api.listCollabRuns())
    } catch (e) {
      setError(describe(e))
    }
  }, [])

  const loadDetail = useCallback(async (id: string) => {
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
      setError(describe(e))
      // 定时刷新遇到网络抖动时保留当前详情；只有运行确实被删除才清空。
      if (e instanceof ApiError && e.status === 404) {
        setDetail(null)
        setTurn(null)
      }
      return null
    }
  }, [dismissedNotices])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  // 离开页面时退订；运行本身在服务端继续，回来再订阅即可。
  useEffect(() => () => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
  }, [])

  // 两台设备看同一次协作时，以落库详情为准定时对齐；本机 SSE 推进期间暂停轮询。
  useAutoRefresh(() => (runId ? loadDetail(runId) : undefined), {
    intervalMs: 3000,
    enabled: Boolean(runId) && !busy,
  })
  useAutoRefresh(loadRuns, { intervalMs: 8000 })

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [visibleSteps.length, chatMessages.length, leaderLive?.agent.text, chatTurn.live?.agent.text, turn?.notices.length])

  /**
   * 订阅一次运行的实时事件并渲染。
   *
   * 推进跑在服务端独立任务里，所以连接被切断（手机锁屏、切后台、隧道抖动）时这里
   * 会自动重连订阅续看，而不是把运行打断；`kickoff` 用于「发消息 / 继续」这类先启动再订阅的场景。
   */
  const subscribe = useCallback(
    async (
      runId: string,
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
          // 第一次订阅（刚打开页面就围观在跑的运行）不算「断线」；只有重连才提示并退避。
          if (attempt > 0 || kickoff) {
            setReconnecting(true)
            await delay(Math.min(800 * attempt, 3000))
            if (controller.signal.aborted) break
          }
          attempt += 1
          await streamGet<RunEvent>(`/api/collab/runs/${runId}/stream`, onEvent, controller.signal)
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describe(e))
      } finally {
        busyRef.current = false
        abortRef.current = null
        setReconnecting(false)
        setBusy(false)
        await loadDetail(runId)
        await loadRuns()
      }
    },
    [loadDetail, loadRuns],
  )

  /** 订阅/发起一次 CEO 聊天；状态与主流程完全分开，主流程在跑也不占用这里。 */
  const subscribeChat = useCallback(
    async (
      runId: string,
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
        let attempt = 0
        while (!closed && !controller.signal.aborted && attempt < 5) {
          attempt += 1
          // 主动发消息后的第一次读取已经由 kickoff 完成；只有断线才需要补订阅。
          if (attempt === 1 && kickoff) break
          await delay(Math.min(800 * attempt, 3000))
          if (controller.signal.aborted) break
          await streamGet<RunEvent>(`/api/collab/runs/${runId}/chat/stream`, onEvent, controller.signal)
        }
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describe(e))
      } finally {
        chatBusyRef.current = false
        chatAbortRef.current = null
        setChatBusy(false)
        await loadDetail(runId)
        await loadRuns()
      }
    },
    [loadDetail, loadRuns],
  )

  useEffect(() => {
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    if (!runId) {
      setDetail(null)
      setTurn(null)
      setChatTurn(emptyChatTurn())
      return
    }
    setDetail(null)
    setTurn(null)
    setChatTurn(emptyChatTurn())
    setIdleMode('auto')
    setError(null)
    void (async () => {
      const next = await loadDetail(runId)
      // 这次运行在跑就订阅实时事件：不管它是本页发起，还是另一台设备/上次会话发起的。
      if (next && next.status === 'running' && !busyRef.current) void subscribe(runId)
      // 刷新/换设备时，如果 CEO 正好在回复，重新接上聊天流。
      if (next?.chat_active && !chatBusyRef.current) void subscribeChat(runId)
    })()
  }, [runId, loadDetail, subscribe, subscribeChat])

  /** 发消息（或空内容触发重跑）并订阅这一轮的推进。 */
  const stream = async (path: string, body: unknown, userText?: string) => {
    if (!detail) return
    const pending = userText ? { id: `local-user-${Date.now()}`, text: userText } : null
    if (pending) setPendingUser(pending)
    setError(null)
    try {
      await subscribe(detail.id, (onEvent, signal) => streamPost<RunEvent>(path, body, onEvent, signal))
    } finally {
      if (pending) setPendingUser(null)
    }
  }

  /** 把一句话发给入口智能体：主流程在跑时走独立聊天通道，否则走正常推进。 */
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

  const start = async () => {
    if (!startTeamId) {
      setError('请选择对应的协作团队')
      return
    }
    try {
      const run = await api.createCollabRun({
        team_id: startTeamId,
        title: startTitle.trim() || undefined,
        workspace_dir: startWorkspace.trim() || null,
      })
      setPicking(false)
      setStartTitle('')
      setStartWorkspace('')
      await loadRuns()
      onOpenRun(run.id)
    } catch (e) {
      setError(describe(e))
    }
  }

  const stop = async () => {
    if (!detail) return
    abortRef.current?.abort()
    chatAbortRef.current?.abort()
    await api.cancelCollabRun(detail.id).catch(() => undefined)
    await loadDetail(detail.id)
  }

  /** 关掉一条提醒：立即从待处理区移除，并记住「用户已经看过、不用再提」。 */
  const dismissNotice = async (notice: CollabNotice) => {
    const current = runId ?? detail?.id ?? ''
    setTurn((prev) => (prev ? dismissCollabNotice(prev, notice.key) : prev))
    rememberDismissedNotice(current, notice.key)
  }

  const retryAndResume = async (stepId: string) => {
    if (!detail) return
    try {
      await api.retryCollabStep(stepId)
    } catch (e) {
      setError(describe(e))
      return
    }
    await stream(`/api/collab/runs/${detail.id}/resume`, {})
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

  const agentAvatar = (agentId?: string | null, nodeKey?: string) => {
    const node = team?.graph.nodes.find((item) => item.key === nodeKey)
    const agent = agents.find((item) => item.id === (agentId ?? node?.agent_profile_id))
    return agent?.avatar ?? '🤖'
  }
  const labelOf = (nodeKey: string, fallback?: string | null) =>
    team?.graph.nodes.find((item) => item.key === nodeKey)?.label || fallback || nodeKey

  const persistedIds = useMemo(() => new Set(visibleSteps.map((step) => step.id)), [visibleSteps])
  const streamedLeader = useMemo(
    () => (turn?.completed ?? []).filter((step) => step.nodeKey === leaderKey && !persistedIds.has(step.stepId)),
    [leaderKey, persistedIds, turn?.completed],
  )
  const timeline: RenderMessage[] = [
    ...[
      ...visibleSteps.map((step) => ({
        at: step.started_at,
        message: step.role_key === 'user'
          ? { id: step.id, role: 'user' as const, content: step.content, parts: [], status: 'complete' }
          : {
              id: step.id,
              role: 'assistant' as const,
              content: step.content,
              parts: step.parts,
              status: step.status,
              error: step.error,
              usage: step.usage,
            },
      })),
      ...chatMessages.map((row) => ({
        at: row.created_at,
        message: row.sender === 'user'
          ? { id: row.id, role: 'user' as const, content: row.content, parts: [], status: 'complete' }
          : {
              id: row.id,
              role: 'assistant' as const,
              content: row.content,
              parts: row.parts,
              status: row.status,
              error: row.error,
              usage: row.usage,
            },
      })),
    ]
      .sort((left, right) => String(left.at).localeCompare(String(right.at)))
      .map((item) => item.message),
    ...(pendingUser
      ? [{ id: pendingUser.id, role: 'user' as const, content: pendingUser.text, parts: [], status: 'complete' }]
      : []),
    ...streamedLeader.map((step) => ({
      id: step.stepId,
      role: 'assistant' as const,
      content: step.agent.text,
      parts: step.agent.parts,
      status: step.error ? 'error' : step.status,
      error: step.error ?? step.agent.error,
      usage: step.agent.usage,
    })),
  ]

  const liveMessage: RenderMessage | null = leaderLive
    ? {
        id: `live-${leaderLive.stepId}`,
        role: 'assistant',
        content: leaderLive.agent.text,
        parts: leaderLive.agent.parts,
        status: leaderLive.agent.error ? 'error' : 'streaming',
        error: leaderLive.agent.error,
        usage: leaderLive.agent.usage,
        streaming: true,
      }
    : null

  const chatLiveMessage: RenderMessage | null = chatTurn.live
    ? {
        id: `chat-live-${chatTurn.live.messageId}`,
        role: 'assistant',
        content: chatTurn.live.agent.text,
        parts: chatTurn.live.agent.parts,
        status: chatTurn.live.agent.error ? 'error' : 'streaming',
        error: chatTurn.live.agent.error,
        usage: chatTurn.live.agent.usage,
        streaming: true,
      }
    : null

  const threadItems: ConversationThreadItem[] = [
    ...timeline.map((message) => ({
      id: message.id,
      message,
      avatar: message.role === 'user' ? undefined : agentAvatar(undefined, leaderKey),
      label: message.role === 'user' ? undefined : labelOf(leaderKey, detail?.entry_key),
    })),
    ...(liveMessage && leaderLive
      ? [{
          id: liveMessage.id,
          message: liveMessage,
          avatar: agentAvatar(undefined, leaderLive.nodeKey),
          label: labelOf(leaderLive.nodeKey, leaderLive.agentName),
        }]
      : []),
    ...(chatLiveMessage && chatTurn.live
      ? [{
          id: chatLiveMessage.id,
          message: chatLiveMessage,
          avatar: agentAvatar(undefined, chatTurn.live.roleKey || leaderKey),
          label: labelOf(chatTurn.live.roleKey || leaderKey, chatTurn.live.agentName),
        }]
      : []),
  ]

  const status = turn?.status ?? detail?.status ?? 'waiting_user'
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
  const progressDone = progress.nodes.filter((node) => node.state === 'done').length
  const progressLive = progress.nodes.some((node) => node.isLive)
  const progressAttention = progress.nodes.some(
    (node) => node.state === 'waiting' || node.state === 'error' || node.state === 'active',
  )
  const besideLeader = turn?.live && turn.live.nodeKey !== leaderKey ? turn.live : null
  const remoteRunning = status === 'running' && !busy
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
  const runUsage = summarizeUsage([detail?.usage])
  // 顶栏的工单状态摘要：不用打开抽屉也能一眼看出卡在哪
  const ticketCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const ticket of detail?.tickets ?? []) counts[ticket.state] = (counts[ticket.state] ?? 0) + 1
    return counts
  }, [detail])

  return (
    <div className="collab-shell">
      <aside className="collab-list">
        <button className="btn primary" style={{ justifyContent: 'center' }} onClick={() => setPicking(true)}>
          + 新建协作任务
        </button>
        {runs.map((run) => (
          <button
            key={run.id}
            className={`collab-item${run.id === runId ? ' active' : ''}`}
            onClick={() => onOpenRun(run.id)}
          >
            <div className="chat-item-title">
              <span className={`pill ${RUN_STATUS[run.status]?.pill ?? ''}`}>{RUN_STATUS[run.status]?.label ?? run.status}</span>
              {run.title}
            </div>
            <div className="chat-item-meta">
              <span>{run.team_name}</span>
              <span>·</span>
              <span>{run.steps_used} 步 / {run.ticket_count} 张工单</span>
            </div>
          </button>
        ))}
        {runs.length === 0 && (
          <p className="dim small" style={{ padding: '14px 8px', lineHeight: 1.6 }}>
            还没有协作运行。先在「协作设置」里画一张编排图，再回来发起。
          </p>
        )}
      </aside>

      <section className="collab-main">
        {detail ? (
          <>
            <div className="collab-topbar">
              <div style={{ minWidth: 0 }}>
                <div className="row" style={{ gap: 8 }}>
                  <span className="collab-run-title">{detail.title}</span>
                  <span className={`pill ${RUN_STATUS[status]?.pill ?? ''}`}>{RUN_STATUS[status]?.label ?? status}</span>
                </div>
                <div className="dim small">
                  {detail.team_name} · 已用 {detail.steps_used}/{detail.budget?.max_steps ?? '—'} 步 ·{' '}
                  {detail.tickets_used}/{detail.budget?.max_tickets ?? '—'} 张工单
                  {detail.budget?.max_rounds_per_ticket
                    ? ` · 单工单最多返工 ${detail.budget.max_rounds_per_ticket} 轮`
                    : ''}
                  {runUsage.total > 0 && ` · ${runUsage.total.toLocaleString()} tok`}
                </div>
                {detail.tickets.length > 0 && (
                  <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
                    {Object.entries(ticketCounts).map(([state, count]) => (
                      <span key={state} className={`pill ${ticketStateMeta(state).pill}`}>
                        {ticketStateMeta(state).label} {count}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <div className="spacer" style={{ flex: 1 }} />
              <div className="row" style={{ gap: 6 }}>
                <button
                  className={`btn sm collab-progress-trigger${progressAttention ? ' attention' : ''}`}
                  onClick={() => setProgressOpen(true)}
                  title="查看岗位推进、实时状态与内部对话入口"
                >
                  <span>🧭 协作进度</span>
                  <span className="collab-progress-trigger-count">{progressDone}/{progress.nodes.length}</span>
                  {progressLive && <span className="collab-progress-trigger-dot" aria-label="有岗位正在实时输出" />}
                </button>
                <button className="btn sm" onClick={() => setBoardOpen(true)}>
                  📋 工单看板 {detail.tickets.length}
                </button>
                {busy || remoteRunning ? (
                  <button className="btn sm danger" onClick={() => void stop()}>
                    {busy ? '■ 停止' : '■ 叫停'}
                  </button>
                ) : (
                  (status === 'waiting_user' || status === 'interrupted' || status === 'error') && (
                    <button className="btn sm" disabled={chatBusy} onClick={() => void stream(`/api/collab/runs/${detail.id}/resume`, {})}>
                      ▶ 继续
                    </button>
                  )
                )}
                <button
                  className="btn sm danger"
                  onClick={async () => {
                    if (!confirm(`删除这次协作「${detail.title}」？工单与时间线都会一起删除。`)) return
                    try {
                      await api.deleteCollabRun(detail.id)
                    } catch (e) {
                      setError(describe(e))
                      return
                    }
                    forgetDismissedNotices(detail.id)
                    onOpenRun(null)
                    await loadRuns()
                  }}
                >
                  删除
                </button>
              </div>
            </div>

            {turn && turn.notices.filter((notice) => notice.level !== 'ask').length > 0 && (
              <div className="stack" style={{ gap: 6, padding: '0 22px 6px' }}>
                {turn.notices.filter((notice) => notice.level !== 'ask').slice(-4).map((notice) => (
                  <div key={notice.key} className={`banner ${notice.level === 'error' ? 'err' : 'info'}`}>
                    <div className="row" style={{ gap: 8 }}>
                      <span style={{ flex: 1 }}>{notice.text}</span>
                      {notice.level === 'error' && notice.stepId && !busy && !chatBusy && (
                        <button className="btn sm" onClick={() => void retryAndResume(notice.stepId!)}>
                          重试这一步
                        </button>
                      )}
                      <button
                        className="banner-close"
                        aria-label="关掉这条提醒"
                        title="关掉这条提醒（状态没变化时，刷新后不会再出现）"
                        onClick={() => void dismissNotice(notice)}
                      >
                        ✕
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {reconnecting && (
              <div style={{ padding: '8px 22px 0' }}>
                <div className="banner info">
                  本页的连接断了，正在自动重连订阅；协作仍在服务端继续推进，不会中断。
                </div>
              </div>
            )}

            {besideLeader && (
              <div style={{ padding: '8px 22px 0' }}>
                <div className="banner info">
                  协作正在由「{besideLeader.agentName}」处理，可在「协作进度」按钮里点开查看实时输出。
                </div>
              </div>
            )}

            <div className="collab-scroll" ref={scrollRef}>
              <ConversationThread
                centered
                items={threadItems}
                empty={timeline.length === 0 && !liveMessage ? (
                  <div className="empty">
                    <div className="empty-icon">🤝</div>
                    <p>这次协作还没有开始</p>
                    <p className="dim small" style={{ marginTop: 6 }}>
                      在下面告诉入口智能体「{detail.entry_key}」你想做什么
                    </p>
                  </div>
                ) : undefined}
              />
            </div>

            {pendingAsks.length > 0 && (
              <div className="collab-ask-stack">
                {pendingAsks.map((ask) => (
                  <div key={ask.ticketId} className="collab-ask-card">
                    <div className="collab-ask-head">
                      <span className="pill warn">需要你决定</span>
                      <span className="dim small">入口智能体在等你的答复</span>
                    </div>
                    <div className="collab-ask-question">{ask.question}</div>
                    <div className="collab-ask-options">
                      {ask.options.map((choice, index) => (
                        <button
                          key={`${ask.ticketId}-${index}-${choice.label}`}
                          className={`collab-ask-option${choice.freeform ? ' freeform' : ''}`}
                          disabled={busy || chatBusy}
                          onClick={() => answerAsk(choice)}
                        >
                          <span className="collab-ask-option-label">{choice.label}</span>
                          {choice.description && (
                            <span className="collab-ask-option-desc">{choice.description}</span>
                          )}
                        </button>
                      ))}
                    </div>
                    <div className="dim small">也可以直接在下方输入其他答复。</div>
                  </div>
                ))}
              </div>
            )}

            {error && (
              <div style={{ padding: '0 22px 6px' }}>
                <div className="banner err">{error}</div>
              </div>
            )}

            {chatTurn.notice && (
              <div style={{ padding: '0 22px 6px' }}>
                <div className={`banner ${chatTurn.notice.level === 'error' ? 'err' : chatTurn.notice.level === 'warn' ? 'warn' : 'info'}`}>
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

            <div className="composer">
              <div className="composer-inner">
                <div className="composer-box">
                  <textarea
                    ref={composerRef}
                    rows={1}
                    value={draft}
                    placeholder={
                      chatBusy
                        ? 'CEO 正在回复…'
                        : chatActive
                          ? '和 CEO 聊聊：问进度、补要求、随便说两句…'
                        : status === 'running'
                          ? '随时问 CEO：细化要求、问进度、聊两句…'
                        : detail.goal
                          ? '推进任务：回答提问，或让 CEO 继续推进工单…'
                          : '告诉领导者这次协作要做什么…'
                    }
                    onChange={(e) => {
                      setDraft(e.target.value)
                      const node = e.target
                      node.style.height = 'auto'
                      node.style.height = `${Math.min(node.scrollHeight, 200)}px`
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                        e.preventDefault()
                        if (!chatBusy && draft.trim()) {
                          const text = draft
                          setDraft('')
                          void sendMessage(text)
                        }
                      }
                    }}
                    disabled={chatBusy}
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
                    {chatBusy ? '回复中…' : '发送 ↵'}
                  </button>
                </div>
                <div className="composer-hint">
                  <span className="row" style={{ gap: 8, alignItems: 'center' }}>
                    {idle && (
                      <button
                        type="button"
                        className={`btn sm${chatActive ? ' primary' : ' ghost'}`}
                        onClick={() => setIdleMode(chatActive ? 'work' : 'chat')}
                        title={
                          chatActive
                            ? '切到推进任务：发送后会继续跑工单'
                            : '切到只聊天：不推进工单，只和 CEO 对话'
                        }
                      >
                        {chatActive ? '💬 聊天中 · 点此推进任务' : '▶ 推进任务 · 点此只聊天'}
                      </button>
                    )}
                    <span>
                      {chatBusy
                        ? 'CEO 正在整理回复…'
                        : chatActive
                          ? pendingAsks.length > 0
                            ? '聊天模式：上方有待回答的问题；回答请点选项，或切到推进任务'
                            : '聊天模式：不推进工单；明确补充的要求会在下一轮步骤生效'
                          : status === 'running'
                            ? 'CEO 在线：任务在跑也能随时问进度、补要求；内部输出在「协作进度」里查看'
                            : detail.goal
                              ? '推进模式：发送后会继续跑工单；想纯聊天点上面的按钮'
                              : '主会话只展示你和领导者；内部智能体输出在「协作进度」里查看'}
                    </span>
                  </span>
                  <span>工作目录：{detail.workspace_dir ?? '默认'}</span>
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="empty" style={{ margin: 'auto' }}>
            <div className="empty-icon">🤝</div>
            <p>选择一次协作，或新建一次</p>
              <button className="btn primary" style={{ marginTop: 14 }} onClick={() => setPicking(true)}>
              + 新建协作任务
            </button>
          </div>
        )}
      </section>

      {boardOpen && detail && (
        <TicketBoard
          tickets={detail.tickets}
          steps={detail.steps}
          graph={team?.graph ?? null}
          agents={agents}
          nodeLabel={(key) => labelOf(key, team?.graph.nodes.find((item) => item.key === key)?.label)}
          runTitle={detail.title}
          onClose={() => setBoardOpen(false)}
        />
      )}

      {progressOpen && detail && (
        <CollabProgressBoard
          nodes={progress.nodes}
          edges={progress.edges}
          steps={detail.steps}
          tickets={detail.tickets}
          live={turn?.live ?? null}
          streamed={turn?.completed ?? []}
          runTitle={detail.title}
          teamName={detail.team_name ?? team?.name ?? '协作组'}
          statusLabel={RUN_STATUS[status]?.label ?? status}
          statusPill={RUN_STATUS[status]?.pill ?? ''}
          onClose={() => setProgressOpen(false)}
        />
      )}

      {picking && (
        <Modal
          title="新建协作任务"
          onClose={() => setPicking(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setPicking(false)}>
                取消
              </button>
              <button className="btn primary" onClick={() => void start()} disabled={!startTeamId}>
                创建并开始对话
              </button>
            </>
          }
        >
          {teams.length === 0 ? (
            <div className="banner info">还没有协作组。请先到「协作设置」页画一张编排图。</div>
          ) : (
            <div className="stack" style={{ gap: 12 }}>
              <label className="field">
                <span className="field-label">协作团队</span>
                <select
                  className="select"
                  value={startTeamId}
                  onChange={(event) => setStartTeamId(event.target.value)}
                >
                  <option value="">请选择…</option>
                  {teams.map((item) => (
                    <option key={item.id} value={item.id} disabled={!item.validation.ok}>
                      {item.name}（{item.node_count} 个岗位）
                      {item.validation.ok ? '' : ' · 暂不可运行'}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">任务标题</span>
                <input
                  className="input"
                  value={startTitle}
                  onChange={(event) => setStartTitle(event.target.value)}
                  placeholder="例如：把登录页改成暗色主题"
                />
              </label>
              <label className="field">
                <span className="field-label">工作路径</span>
                <input
                  className="input mono"
                  value={startWorkspace}
                  onChange={(event) => setStartWorkspace(event.target.value)}
                  placeholder="例如 /vol1/projects/my-app；留空用默认路径"
                />
                <span className="field-help">
                  {teams.find((item) => item.id === startTeamId)?.workspace_dir
                    ? `留空则使用团队默认路径：${teams.find((item) => item.id === startTeamId)?.workspace_dir}`
                    : '留空则使用每次运行自动分配的默认路径。填写后所有智能体只能在该路径内读写。'}
                </span>
              </label>
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
