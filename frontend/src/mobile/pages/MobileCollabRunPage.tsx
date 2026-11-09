import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { MessageView, type RenderMessage } from '../../components/MessageView'
import { api, streamPost } from '../../lib/api'
import { applyCollabEvent, makeCollabTurn, type CollabTurnState } from '../../lib/collabStream'
import type { CollabEvent, CollabRunDetail, CollabTeam, Role } from '../../lib/types'
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

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const load = useCallback(async (id: string) => {
    try {
      const next = await api.getCollabRun(id)
      setDetail(next)
      setTurn((prev) =>
        prev
          ? { ...prev, status: next.status, reason: next.error ?? null, live: null }
          : makeCollabTurn(next, next.tickets),
      )
      setError(null)
    } catch (e) {
      setError(describeError(e))
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

  useEffect(() => {
    if (runId) void load(runId)
  }, [runId, load])

  useEffect(() => () => abortRef.current?.abort(), [])

  // 运行时定时对齐后端详情：SSE 只负责增量，最终结果以落库详情为准。
  useEffect(() => {
    if (!runId || !detail) return
    if (detail.status !== 'running' && detail.status !== 'waiting_user') return
    const timer = setInterval(() => void load(runId), 5000)
    return () => clearInterval(timer)
  }, [runId, detail, load])

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [detail?.steps.length, turn?.live?.agent.text])

  const stream = useCallback(
    async (path: string, body: unknown) => {
      if (!detail) return
      setError(null)
      setBusy(true)
      const controller = new AbortController()
      abortRef.current = controller
      try {
        await streamPost<CollabEvent>(path, body, (event) =>
          setTurn((prev) => (prev ? applyCollabEvent(prev, event) : prev)),
          controller.signal,
        )
      } catch (e) {
        if ((e as Error)?.name !== 'AbortError') setError(describeError(e))
      } finally {
        abortRef.current = null
        setBusy(false)
        await load(detail.id)
      }
    },
    [detail, load],
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

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])

  const timeline = useMemo<TimelineEntry[]>(() => {
    if (!detail) return []
    return detail.steps.map((step) => {
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
  }, [detail, team, roles])

  const liveEntry: TimelineEntry | null = turn?.live
    ? {
        id: 'live',
        avatar: roles.find(
          (role) => role.id === team?.graph.nodes.find((node) => node.key === turn.live!.nodeKey)?.role_id,
        )?.avatar ?? '🤖',
        label: `${turn.live.roleName} · ${turn.live.duty}`,
        message: {
          id: 'live',
          role: 'assistant',
          content: turn.live.agent.text,
          parts: turn.live.agent.parts,
          status: turn.live.agent.error ? 'error' : 'streaming',
          error: turn.live.agent.error,
          usage: turn.live.agent.usage,
          streaming: turn.live.agent.status === 'running',
          logs: turn.live.agent.logs,
        },
      }
    : null

  const status = detail?.status ?? 'running'
  const canResume = ['waiting_user', 'error', 'interrupted', 'cancelled'].includes(status)
  const canReply = status === 'waiting_user'

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

      {error && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="banner err">{error}</div>
        </div>
      )}

      {turn && turn.notices.length > 0 && (
        <div style={{ padding: '8px 12px 0' }}>
          <div className="stack" style={{ gap: 6 }}>
            {turn.notices.slice(-3).map((notice) => (
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

      <div className="m-thread" ref={scrollRef}>
        {timeline.length === 0 && !liveEntry && !busy && (
          <div className="m-empty">
            <div className="m-empty-icon">🤝</div>
            <p>运行尚未产生步骤</p>
            <p className="dim small">点下方按钮推进协作。</p>
          </div>
        )}
        {timeline.map((entry) => (
          <MessageView key={entry.id} message={entry.message} avatar={entry.avatar} label={entry.label} />
        ))}
        {liveEntry && <MessageView message={liveEntry.message} avatar={liveEntry.avatar} label={liveEntry.label} />}
      </div>

      <div className="m-composer">
        {canReply ? (
          <div className="m-composer-box">
            <textarea
              rows={1}
              value={draft}
              disabled={busy}
              placeholder="回答入口岗位的问题…"
              onChange={(event) => setDraft(event.target.value)}
            />
            <button
              className="btn primary"
              disabled={busy || !draft.trim()}
              onClick={() => {
                const text = draft
                setDraft('')
                void stream(`/api/collab/runs/${detail?.id}/messages`, { content: text })
              }}
            >
              回复
            </button>
          </div>
        ) : (
          <div className="row" style={{ gap: 8 }}>
            {canResume && (
              <button
                className="btn primary"
                style={{ flex: 1 }}
                disabled={busy}
                onClick={() => void stream(`/api/collab/runs/${detail?.id}/resume`, {})}
              >
                {busy ? '推进中…' : '继续推进'}
              </button>
            )}
            {status === 'running' && (
              <button className="btn danger" style={{ flex: 1 }} disabled={busy} onClick={() => void stop()}>
                ■ 叫停
              </button>
            )}
            {status === 'done' && <span className="dim small">本次协作已完成。</span>}
          </div>
        )}
      </div>

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
