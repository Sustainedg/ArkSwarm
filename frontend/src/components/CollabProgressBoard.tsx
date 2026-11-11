/** 协作进度（全屏）：展示完成率、岗位状态与流转关系，并在页面内展开岗位实时对话。 */
import { useEffect, useState } from 'react'
import { DUTIES } from '../lib/collabDraft'
import type { FinishedStep, LiveStep } from '../lib/collabStream'
import type { CollabFlowEdge, CollabRoleProgress } from '../lib/collabProgress'
import type { CollabStep, CollabTicket } from '../lib/types'
import { RoleConversationPanel } from './RoleConversationPanel'

interface Props {
  nodes: CollabRoleProgress[]
  edges: CollabFlowEdge[]
  steps: CollabStep[]
  tickets: CollabTicket[]
  live: LiveStep | null
  streamed: FinishedStep[]
  runTitle: string
  teamName: string
  statusLabel: string
  statusPill: string
  onClose: () => void
}

const STATE_ACCENT: Record<CollabRoleProgress['state'], string> = {
  pending: '待启动',
  active: '推进中',
  waiting: '等你决定',
  error: '需处理',
  done: '已完成',
}

function dutyLabel(duty: string): string {
  return DUTIES.find((item) => item.value === duty)?.label ?? duty
}

export function CollabProgressBoard({
  nodes, edges, steps, tickets, live, streamed, runTitle, teamName, statusLabel, statusPill, onClose,
}: Props) {
  const [selectedRoleKey, setSelectedRoleKey] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (selectedRoleKey) setSelectedRoleKey(null)
      else onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, selectedRoleKey])

  if (nodes.length === 0) return null

  const selectedRole = nodes.find((node) => node.key === selectedRoleKey) ?? null
  const total = nodes.length
  const done = nodes.filter((node) => node.state === 'done').length
  const active = nodes.filter((node) => node.state === 'active').length
  const waiting = nodes.filter((node) => node.state === 'waiting').length
  const errors = nodes.filter((node) => node.state === 'error').length
  const completion = Math.round((done / total) * 100)
  const focus =
    nodes.find((node) => node.isLive) ??
    nodes.find((node) => node.state === 'waiting') ??
    nodes.find((node) => node.state === 'active') ??
    nodes.find((node) => node.state === 'error') ??
    nodes.find((node) => node.state !== 'done')

  const overviewTitle = done === total
    ? '全部岗位已完成'
    : focus
      ? `当前焦点：${focus.label}`
      : '等待下一步推进'
  const overviewSummary = focus?.summary ?? '本次协作暂时没有待处理的岗位。'

  return (
    <div className="collab-drawer-backdrop collab-progress-board-backdrop">
      <section className="collab-progress-board" role="dialog" aria-modal="true" aria-label="协作进度">
        <header className="collab-progress-board-head">
          <div>
            <div className="collab-progress-eyebrow">协作流程</div>
            <div className="collab-progress-title-row">
              <h2>协作进度</h2>
              <span className={`pill ${statusPill}`}>{statusLabel}</span>
            </div>
            <p className="collab-progress-subtitle">{runTitle} · {teamName}</p>
          </div>
          <button className="btn ghost sm collab-progress-close" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </header>

        <div className={`collab-progress-board-body${selectedRole ? ' has-conversation' : ''}`}>
          <div className="collab-progress-board-main">
            <section className="collab-progress-overview" aria-label="运行概览">
              <div
                className="collab-progress-ring"
                style={{ background: `conic-gradient(var(--brand) ${completion}%, var(--border) 0 100%)` }}
              >
                <div className="collab-progress-ring-inner">
                  <strong>{completion}%</strong>
                  <span>岗位完成</span>
                </div>
              </div>
              <div className="collab-progress-overview-main">
                <div className="collab-progress-overview-kicker">本次协作概览</div>
                <div className="collab-progress-overview-title">{overviewTitle}</div>
                <div className="collab-progress-overview-summary">{overviewSummary}</div>
                <div className="collab-progress-stats">
                  <div className="collab-progress-stat state-active"><strong>{active}</strong><span>推进中</span></div>
                  <div className="collab-progress-stat state-waiting"><strong>{waiting}</strong><span>等你决定</span></div>
                  <div className="collab-progress-stat state-done"><strong>{done}</strong><span>已完成</span></div>
                  <div className="collab-progress-stat state-error"><strong>{errors}</strong><span>需处理</span></div>
                </div>
              </div>
            </section>

            <section className="collab-progress-section">
              <div className="collab-progress-section-head">
                <div>
                  <h3>岗位推进</h3>
                  <p>点击任意岗位，右侧会直接展开它在本次协作里的实时对话。</p>
                </div>
                <span className="collab-progress-count">{done}/{total} 个岗位已完成</span>
              </div>
              <div className="collab-progress-stage-list">
                {nodes.map((node, index) => (
                  <button
                    key={node.key}
                    className={`collab-progress-stage state-${node.state}${node.isLive ? ' live' : ''}${node.key === selectedRoleKey ? ' selected' : ''}`}
                    onClick={() => setSelectedRoleKey(node.key)}
                    title="在进度页内查看这个岗位的实时对话"
                  >
                    <span className="collab-progress-stage-index">{String(index + 1).padStart(2, '0')}</span>
                    <span className="collab-progress-stage-head">
                      <span className="collab-progress-stage-avatar">{node.avatar}</span>
                      <span className="collab-progress-stage-identity">
                        <span className="collab-progress-stage-name">{node.label}</span>
                        <span className="collab-progress-stage-duty">
                          {dutyLabel(node.duty)}
                          {node.isEntry ? ' · 领导者' : ''}
                        </span>
                      </span>
                      <span className="collab-progress-stage-state">{node.stateLabel}</span>
                    </span>
                    <span className="collab-progress-stage-meta">
                      {node.stepCount > 0 ? `${node.stepCount} 步` : '尚未参与'}
                      {node.lastAction ? ` · ${node.lastAction}` : ''}
                      {node.activeTickets.length > 0 ? ` · ${node.activeTickets.length} 张在处理` : ''}
                    </span>
                    <span className="collab-progress-stage-summary">{node.summary}</span>
                    <span className="collab-progress-stage-foot">
                      <span className={`collab-progress-stage-badge state-${node.state}`}>
                        {STATE_ACCENT[node.state]}
                      </span>
                      <span>{node.key === selectedRoleKey ? '正在查看对话' : '查看实时对话 →'}</span>
                    </span>
                  </button>
                ))}
              </div>
            </section>

            {edges.length > 0 && (
              <section className="collab-progress-section collab-progress-flows">
                <div className="collab-progress-section-head">
                  <div>
                    <h3>协作流向</h3>
                    <p>这些连线决定工单可以沿哪个方向流转，以及审查失败时往哪里打回。</p>
                  </div>
                </div>
                <div className="collab-progress-flow-list">
                  {edges.map((edge) => (
                    <span className={`collab-progress-flow kind-${edge.kind}`} key={edge.id}>
                      <span>{edge.from}</span>
                      <b>{edge.kindLabel}</b>
                      <span>{edge.to}</span>
                    </span>
                  ))}
                </div>
              </section>
            )}
          </div>

          {selectedRole && (
            <aside className="collab-progress-conversation">
              <RoleConversationPanel
                node={selectedRole}
                roster={nodes}
                steps={steps}
                tickets={tickets}
                live={live}
                streamed={streamed}
                onClose={() => setSelectedRoleKey(null)}
                closeLabel="← 返回概览"
                variant="embedded"
              />
            </aside>
          )}
        </div>
      </section>
    </div>
  )
}
