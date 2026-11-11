import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../../components/MessageView'
import { api, streamGet, streamPost } from '../../lib/api'
import { collabChoiceAnswer, findPendingCollabAsks } from '../../lib/collabAsk'
import { applyCollabEvent, makeCollabTurn, type CollabTurnState } from '../../lib/collabStream'
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
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [selectedRoleKey, setSelectedRoleKey] = useState<string | null>(null)
  const [pendingUser, setPendingUser] = useState<{ id: string; text: string } | null>(null)
  const [reconnecting, setReconnecting] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const busyRef = useRef(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const composerRef = useRef<HTMLTextAreaElement | null>(null)

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])
  const leaderKey = detail?.entry_key ?? team?.entry_key ?? ''
  const leaderLabel = team?.graph.nodes.find((node) => node.key === leaderKey)?.label || leaderKey || '领导者'
  const visibleSteps = useMemo(
    () => (detail?.steps ?? []).filter((step) => step.role_key === 'user' || step.role_key === leaderKey),
    [detail?.steps, leaderKey],
  )
  const leaderLive = turn?.live?.nodeKey === leaderKey ? turn.live : null

  const load = useCallback(async (id: string) => {
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
      setError(describeError(e))
      return null
    }
  }, [])

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
  useEffect(() => () => abortRef.current?.abort(), [])

  // SSE 只负责本设备发起的增量；运行详情以落库结果为准，并定时对齐其他设备。
  useAutoRefresh(() => (runId ? load(runId) : undefined), {
    intervalMs: 4000,
    enabled: Boolean(runId) && !busy,
  })

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [visibleSteps.length, leaderLive?.agent.text])

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

  useEffect(() => {
    abortRef.current?.abort()
    if (!runId) return
    setError(null)
    setSelectedRoleKey(null)
    void (async () => {
      const next = await load(runId)
      // 这次运行在跑就订阅实时事件：本页发起的、或别的设备/上次会话发起的都一样。
      if (next && next.status === 'running' && !busyRef.current) void subscribe(runId)
    })()
  }, [runId, load, subscribe])

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
    if (!detail || busy) return
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
    // 尽力而为：本地流已经断开，服务端叫停失败不影响用户（引擎会按预算自行收尾）。
    await api.cancelCollabRun(detail.id).catch(() => undefined)
    await load(detail.id)
  }

  const remove = async () => {
    if (!detail) return
    setMenuOpen(false)
    if (!confirm(`删除运行「${detail.title}」及其全部工单与步骤？`)) return
    await api.deleteCollabRun(detail.id)
    navigate('/collab', { replace: true })
  }

  const timeline = useMemo<TimelineEntry[]>(() => {
    if (!detail) return []
    const entries = visibleSteps.map((step): TimelineEntry => {
      if (step.role_key === 'user') {
        return {
          id: step.id,
          label: '你',
          message: { id: step.id, role: 'user', content: step.content, parts: [], status: 'complete' },
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
          role: 'assistant',
          content: step.content,
          parts: step.parts,
          status: step.status,
          error: step.error,
          usage: step.usage,
        },
      }
    })
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
  }, [detail, team, roles, visibleSteps, pendingUser, turn?.completed, leaderKey])

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
              <div className={`banner${notice.level === 'error' ? ' err' : ''}`} key={notice.id}>
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

      <div className="m-thread" ref={scrollRef}>
        {timeline.length === 0 && !liveEntry && pendingAsks.length === 0 && !busy && (
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
                  disabled={busy}
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
        {remoteRunning ? (
          <div className="m-collab-running-bar">
            <span className="dim small">
              领导者团队正在推进{turn?.live ? `「${turn.live.roleName}」` : ''}，完成后可继续对话
            </span>
            <button className="btn sm danger" onClick={() => void stop()}>
              叫停
            </button>
          </div>
        ) : (
          <>
            <div className="m-composer-box">
              <textarea
                ref={composerRef}
                rows={1}
                value={draft}
                disabled={busy}
                placeholder={busy ? '协作正在推进…' : `和领导者「${leaderLabel}」对话…`}
                onChange={(event) => setDraft(event.target.value)}
              />
              {busy ? (
                <button className="btn danger" onClick={() => void stop()}>
                  ■ 停止
                </button>
              ) : (
                <button
                  className="btn primary"
                  disabled={!draft.trim()}
                  onClick={() => {
                    const text = draft
                    setDraft('')
                    void stream(`/api/collab/runs/${detail?.id}/messages`, { content: text }, text)
                  }}
                >
                  发送
                </button>
              )}
            </div>
            {canResume && !busy && (
              <button
                className="btn ghost block m-composer-secondary"
                onClick={() => void stream(`/api/collab/runs/${detail?.id}/resume`, {})}
              >
                让领导者继续推进（不补充内容）
              </button>
            )}
          </>
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
