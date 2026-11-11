/**
 * 「协作运行」页签：左栏是运行列表，主区是时间线（复用 `MessageView`）与工单状态摘要，
 * 底部输入框直接和入口角色说话（走协作 SSE）。
 *
 * 工单的细节（子工单树、产出文件、审查意见、越权审计）在点击后打开的全屏 `TicketBoard` 里。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CollabProgressBoard } from './CollabProgressBoard'
import { ConversationThread, type ConversationThreadItem } from './ConversationThread'
import type { RenderMessage } from './MessageView'
import { TicketBoard } from './TicketBoard'
import { Modal } from './Modal'
import { api, ApiError, streamGet, streamPost } from '../lib/api'
import { collabChoiceAnswer, findPendingCollabAsks } from '../lib/collabAsk'
import { applyCollabEvent, makeCollabTurn, type CollabTurnState } from '../lib/collabStream'
import { buildCollabProgress } from '../lib/collabProgress'
import { useAutoRefresh } from '../lib/useAutoRefresh'
import { summarizeUsage } from '../lib/usage'
import type {
  CollabChoice, CollabEvent, CollabRun, CollabRunDetail, CollabTeam, Role, StreamClosedEvent,
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

const TICKET_STATE: Record<string, { label: string; pill: string }> = {
  open: { label: '待领取', pill: '' },
  working: { label: '处理中', pill: 'brand' },
  waiting_children: { label: '等子工单', pill: '' },
  review: { label: '待审查', pill: 'ok' },
  rework: { label: '返工中', pill: 'warn' },
  done: { label: '已完成', pill: 'ok' },
  rejected: { label: '被否决', pill: 'err' },
  blocked: { label: '等用户', pill: 'warn' },
}

interface Props {
  runId?: string
  teams: CollabTeam[]
  roles: Role[]
  onOpenRun: (runId: string | null) => void
}

export function CollabRunsPanel({ runId, teams, roles, onOpenRun }: Props) {
  const [runs, setRuns] = useState<CollabRun[]>([])
  const [detail, setDetail] = useState<CollabRunDetail | null>(null)
  const [turn, setTurn] = useState<CollabTurnState | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
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
  const busyRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])
  const leaderKey = detail?.entry_key ?? team?.entry_key ?? ''
  const visibleSteps = useMemo(
    () => (detail?.steps ?? []).filter((step) => step.role_key === 'user' || step.role_key === leaderKey),
    [detail?.steps, leaderKey],
  )
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
        const base = prev ?? makeCollabTurn(next, next.tickets)
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
  }, [])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  // 离开页面时退订；运行本身在服务端继续，回来再订阅即可。
  useEffect(() => () => abortRef.current?.abort(), [])

  // 两台设备看同一次协作时，以落库详情为准定时对齐；本机 SSE 推进期间暂停轮询。
  useAutoRefresh(() => (runId ? loadDetail(runId) : undefined), {
    intervalMs: 3000,
    enabled: Boolean(runId) && !busy,
  })
  useAutoRefresh(loadRuns, { intervalMs: 8000 })

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [visibleSteps.length, leaderLive?.agent.text, turn?.notices.length])

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

  useEffect(() => {
    abortRef.current?.abort()
    if (!runId) {
      setDetail(null)
      setTurn(null)
      return
    }
    setDetail(null)
    setTurn(null)
    setError(null)
    void (async () => {
      const next = await loadDetail(runId)
      // 这次运行在跑就订阅实时事件：不管它是本页发起，还是另一台设备/上次会话发起的。
      if (next && next.status === 'running' && !busyRef.current) void subscribe(runId)
    })()
  }, [runId, loadDetail, subscribe])

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
    await api.cancelCollabRun(detail.id).catch(() => undefined)
    await loadDetail(detail.id)
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
    if (!detail || busy) return
    const text = collabChoiceAnswer(choice)
    if (!text) {
      composerRef.current?.focus()
      return
    }
    setDraft('')
    void stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
  }

  const roleAvatar = (roleId?: string | null, nodeKey?: string) => {
    const node = team?.graph.nodes.find((item) => item.key === nodeKey)
    const role = roles.find((item) => item.id === (roleId ?? node?.role_id))
    return role?.avatar ?? '🤖'
  }
  const roleLabel = (nodeKey: string, fallback?: string | null) =>
    team?.graph.nodes.find((item) => item.key === nodeKey)?.label || fallback || nodeKey

  const persistedIds = useMemo(() => new Set(visibleSteps.map((step) => step.id)), [visibleSteps])
  const streamedLeader = useMemo(
    () => (turn?.completed ?? []).filter((step) => step.nodeKey === leaderKey && !persistedIds.has(step.stepId)),
    [leaderKey, persistedIds, turn?.completed],
  )
  const timeline: RenderMessage[] = [
    ...visibleSteps.map((step) =>
      step.role_key === 'user'
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
    ),
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

  const threadItems: ConversationThreadItem[] = [
    ...timeline.map((message) => ({
      id: message.id,
      message,
      avatar: message.role === 'user' ? undefined : roleAvatar(undefined, leaderKey),
      label: message.role === 'user' ? undefined : roleLabel(leaderKey, detail?.entry_key),
    })),
    ...(liveMessage && leaderLive
      ? [{
          id: liveMessage.id,
          message: liveMessage,
          avatar: roleAvatar(undefined, leaderLive.nodeKey),
          label: roleLabel(leaderLive.nodeKey, leaderLive.roleName),
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
        roles,
        live: turn?.live ?? null,
        streamed: turn?.completed ?? [],
        runStatus: status,
      }),
    [team?.graph, leaderKey, detail?.steps, detail?.tickets, roles, turn?.live, turn?.completed, status],
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
                      <span key={state} className={`pill ${TICKET_STATE[state]?.pill ?? ''}`}>
                        {TICKET_STATE[state]?.label ?? state} {count}
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
                    <button className="btn sm" onClick={() => void stream(`/api/collab/runs/${detail.id}/resume`, {})}>
                      ▶ 继续
                    </button>
                  )
                )}
                <button
                  className="btn sm danger"
                  onClick={async () => {
                    if (!confirm(`删除这次协作「${detail.title}」？工单与时间线都会一起删除。`)) return
                    await api.deleteCollabRun(detail.id)
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
                  <div key={notice.id} className={`banner ${notice.level === 'error' ? 'err' : 'info'}`}>
                    <div className="row" style={{ gap: 8 }}>
                      <span style={{ flex: 1 }}>{notice.text}</span>
                      {notice.level === 'error' && notice.stepId && !busy && (
                        <button className="btn sm" onClick={() => void retryAndResume(notice.stepId!)}>
                          重试这一步
                        </button>
                      )}
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
                  协作正在由「{besideLeader.roleName}」处理，可在「协作进度」按钮里点开查看实时输出。
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
                      在下面告诉入口角色「{detail.entry_key}」你想做什么
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
                      <span className="dim small">入口角色在等你的答复</span>
                    </div>
                    <div className="collab-ask-question">{ask.question}</div>
                    <div className="collab-ask-options">
                      {ask.options.map((choice, index) => (
                        <button
                          key={`${ask.ticketId}-${index}-${choice.label}`}
                          className={`collab-ask-option${choice.freeform ? ' freeform' : ''}`}
                          disabled={busy}
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

            <div className="composer">
              <div className="composer-inner">
                <div className="composer-box">
                  <textarea
                    ref={composerRef}
                    rows={1}
                    value={draft}
                    placeholder={
                      busy || remoteRunning
                        ? '协作正在推进，可在「协作进度」按钮里查看实时输出…'
                        : detail.goal
                          ? `和领导者「${roleLabel(leaderKey, detail.entry_key)}」继续对话…`
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
                        if (!busy && !remoteRunning && draft.trim()) {
                          const text = draft
                          setDraft('')
                          void stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
                        }
                      }
                    }}
                    disabled={busy || remoteRunning}
                  />
                  <button
                    className="btn primary"
                    disabled={busy || remoteRunning || !draft.trim()}
                    onClick={() => {
                      const text = draft
                      setDraft('')
                      void stream(`/api/collab/runs/${detail.id}/messages`, { content: text }, text)
                    }}
                  >
                    发送 ↵
                  </button>
                </div>
                <div className="composer-hint">
                  <span>主会话只展示你和领导者；内部角色输出在「协作进度」按钮里点开查看</span>
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
          nodeLabel={(key) => roleLabel(key, team?.graph.nodes.find((item) => item.key === key)?.label)}
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
                    : '留空则使用每次运行自动分配的默认路径。填写后所有角色只能在该路径内读写。'}
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
