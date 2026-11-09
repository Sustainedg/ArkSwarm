/**
 * 「协作运行」页签：左栏是运行列表，主区是时间线（复用 `MessageView`）与工单状态摘要，
 * 底部输入框直接和入口角色说话（走协作 SSE）。
 *
 * 工单的细节（子工单树、产出文件、审查意见、越权审计）在右侧抽屉 `TicketBoard` 里。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MessageView, type RenderMessage } from './MessageView'
import { TicketBoard } from './TicketBoard'
import { Modal } from './Modal'
import { api, ApiError, streamPost } from '../lib/api'
import { applyCollabEvent, makeCollabTurn, type CollabTurnState } from '../lib/collabStream'
import { summarizeUsage } from '../lib/usage'
import type { CollabEvent, CollabRun, CollabRunDetail, CollabTeam, Role } from '../lib/types'

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
  const [boardOpen, setBoardOpen] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  const team = useMemo(() => teams.find((item) => item.id === detail?.team_id), [teams, detail?.team_id])

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
      setTurn((prev) =>
        prev ? { ...prev, status: next.status, reason: next.error ?? null, live: null } : makeCollabTurn(next, next.tickets),
      )
    } catch (e) {
      setError(describe(e))
      setDetail(null)
    }
  }, [])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  useEffect(() => {
    if (runId) void loadDetail(runId)
    else setDetail(null)
  }, [runId, loadDetail])

  useEffect(() => {
    const node = scrollRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [detail?.steps.length, turn?.live?.agent.text, turn?.notices.length])

  /** 跑一轮 SSE（发消息或继续）；结束后以后端详情为准刷新。 */
  const stream = async (path: string, body: unknown) => {
    if (!detail) return
    setError(null)
    setBusy(true)
    const controller = new AbortController()
    abortRef.current = controller
    try {
      await streamPost<CollabEvent>(
        path,
        body,
        (event) => setTurn((prev) => (prev ? applyCollabEvent(prev, event) : prev)),
        controller.signal,
      )
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') setError(describe(e))
    } finally {
      abortRef.current = null
      setBusy(false)
      await loadDetail(detail.id)
      await loadRuns()
    }
  }

  const start = async (teamId: string, title: string) => {
    try {
      const run = await api.createCollabRun({ team_id: teamId, title: title.trim() || undefined })
      setPicking(false)
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

  const roleAvatar = (roleId?: string | null, nodeKey?: string) => {
    const node = team?.graph.nodes.find((item) => item.key === nodeKey)
    const role = roles.find((item) => item.id === (roleId ?? node?.role_id))
    return role?.avatar ?? '🤖'
  }
  const roleLabel = (nodeKey: string, fallback?: string | null) =>
    team?.graph.nodes.find((item) => item.key === nodeKey)?.label || fallback || nodeKey

  const timeline: RenderMessage[] = detail
    ? detail.steps.map((step) =>
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
      )
    : []

  const liveMessage: RenderMessage | null = turn?.live
    ? {
        id: `live-${turn.live.stepId}`,
        role: 'assistant',
        content: turn.live.agent.text,
        parts: turn.live.agent.parts,
        status: turn.live.agent.error ? 'error' : 'streaming',
        error: turn.live.agent.error,
        usage: turn.live.agent.usage,
        streaming: true,
      }
    : null

  const status = turn?.status ?? detail?.status ?? 'waiting_user'
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
          + 新建协作
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
                <button className="btn sm" onClick={() => setBoardOpen(true)}>
                  📋 工单看板 {detail.tickets.length}
                </button>
                {busy ? (
                  <button className="btn sm danger" onClick={() => void stop()}>
                    ■ 停止
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

            {turn && turn.notices.length > 0 && (
              <div className="stack" style={{ gap: 6, padding: '0 22px 6px' }}>
                {turn.notices.slice(-4).map((notice) => (
                  <div key={notice.id} className={`banner ${notice.level === 'error' ? 'err' : notice.level === 'ask' ? 'info' : 'info'}`}>
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

            <div className="collab-scroll" ref={scrollRef}>
              <div className="chat-thread">
                {timeline.length === 0 && !liveMessage && (
                  <div className="empty">
                    <div className="empty-icon">🤝</div>
                    <p>这次协作还没有开始</p>
                    <p className="dim small" style={{ marginTop: 6 }}>
                      在下面告诉入口角色「{detail.entry_key}」你想做什么
                    </p>
                  </div>
                )}
                {timeline.map((message, index) => (
                  <MessageView
                    key={message.id}
                    message={message}
                    avatar={message.role === 'user' ? undefined : roleAvatar(detail.steps[index]?.role_id, detail.steps[index]?.role_key)}
                    label={message.role === 'user' ? undefined : roleLabel(detail.steps[index]?.role_key ?? '', detail.steps[index]?.role_name)}
                  />
                ))}
                {liveMessage && turn?.live && (
                  <MessageView message={liveMessage} avatar={roleAvatar(undefined, turn.live.nodeKey)} label={roleLabel(turn.live.nodeKey, turn.live.roleName)} />
                )}
              </div>
            </div>

            {error && (
              <div style={{ padding: '0 22px 6px' }}>
                <div className="banner err">{error}</div>
              </div>
            )}

            <div className="composer">
              <div className="composer-inner">
                <div className="composer-box">
                  <textarea
                    rows={1}
                    value={draft}
                    placeholder={busy ? '协作正在推进…' : detail.goal ? '补充需求 / 回答追问（Enter 发送）' : '这次协作要做什么？'}
                    onChange={(e) => {
                      setDraft(e.target.value)
                      const node = e.target
                      node.style.height = 'auto'
                      node.style.height = `${Math.min(node.scrollHeight, 200)}px`
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                        e.preventDefault()
                        if (!busy && draft.trim()) {
                          const text = draft
                          setDraft('')
                          void stream(`/api/collab/runs/${detail.id}/messages`, { content: text })
                        }
                      }
                    }}
                    disabled={busy}
                  />
                  <button
                    className="btn primary"
                    disabled={busy || !draft.trim()}
                    onClick={() => {
                      const text = draft
                      setDraft('')
                      void stream(`/api/collab/runs/${detail.id}/messages`, { content: text })
                    }}
                  >
                    发送 ↵
                  </button>
                </div>
                <div className="composer-hint">
                  <span>角色之间只传工单，不共享对话记忆</span>
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
              + 新建协作
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

      {picking && (
        <Modal
          title="选择协作组"
          onClose={() => setPicking(false)}
          footer={
            <button className="btn ghost" onClick={() => setPicking(false)}>
              取消
            </button>
          }
        >
          {teams.length === 0 ? (
            <div className="banner info">还没有协作组。请先到「协作设置」页画一张编排图。</div>
          ) : (
            <div className="stack" style={{ gap: 8 }}>
              {teams.map((item) => (
                <div
                  key={item.id}
                  className={`agent-type-row${item.validation.ok ? '' : ' disabled'}`}
                  onClick={() => void start(item.id, '')}
                >
                  <span className="collab-node-avatar">🤝</span>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div className="agent-type-name">{item.name}</div>
                    <div className="agent-type-desc">
                      {item.node_count} 个岗位
                      {item.validation.ok ? ' · 可以运行' : ` · 还不能跑：${item.validation.errors[0]?.message ?? ''}`}
                    </div>
                  </div>
                  <span className={`pill ${item.validation.ok ? 'ok' : 'err'}`}>
                    {item.validation.ok ? '就绪' : '有问题'}
                  </span>
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
