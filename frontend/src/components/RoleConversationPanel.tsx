/** 岗位内部实时对话面板：进度页内嵌与独立抽屉共用同一套外壳，线程本身复用 `CollabRoleThread`。 */
import { useMemo } from 'react'
import { DUTIES } from '../lib/collabDraft'
import type { FinishedStep, LiveStep } from '../lib/collabStream'
import type { CollabRoleProgress } from '../lib/collabProgress'
import type { CollabStep, CollabTicket } from '../lib/types'
import { CollabRoleThread } from './CollabRoleThread'

interface Props {
  node: CollabRoleProgress
  roster: CollabRoleProgress[]
  steps: CollabStep[]
  tickets: CollabTicket[]
  live: LiveStep | null
  streamed: FinishedStep[]
  onClose: () => void
  closeLabel?: string
  variant?: 'embedded' | 'drawer'
}

export function RoleConversationPanel({
  node, roster, steps, tickets, live, streamed, onClose, closeLabel = '关闭', variant = 'drawer',
}: Props) {
  const liveHere = live?.nodeKey === node.key ? live : null
  const dutyLabel = useMemo(
    () => DUTIES.find((item) => item.value === node.duty)?.label ?? node.duty,
    [node.duty],
  )

  return (
    <div className={`collab-role-panel ${variant}`} aria-label={`${node.label}的实时对话`}>
      <header className="collab-role-panel-head">
        <span className="collab-progress-avatar">{node.avatar}</span>
        <div className="collab-role-panel-title">
          <div className="collab-role-panel-name">{node.label} · 实时对话</div>
          <div className="collab-role-panel-meta">
            {dutyLabel} · {node.stepCount} 步 · {node.stateLabel}
            {node.isEntry ? ' · 入口领导者' : ' · 只读内部对话'}
          </div>
        </div>
        {liveHere && <span className="pill brand collab-role-live-pill">实时输出中</span>}
        <button className="btn sm ghost collab-role-panel-close" onClick={onClose}>
          {closeLabel}
        </button>
      </header>

      {node.activeTickets.length > 0 && (
        <div className="collab-role-tickets">
          {node.activeTickets.map((ticket) => (
            <span className="pill" key={ticket.id}>
              当前工单：{ticket.title}
            </span>
          ))}
        </div>
      )}

      <CollabRoleThread
        node={node}
        roster={roster}
        steps={steps}
        tickets={tickets}
        live={live}
        streamed={streamed}
        className="collab-role-thread"
        empty={(
          <div className="collab-role-empty">
            <div className="collab-role-empty-icon">{node.avatar}</div>
            {node.state === 'active' ? (
              <>
                <p>「{node.label}」正在处理中…</p>
                <p className="dim small">输出会在这一步跑完后显示；由本页发起时这里也会实时刷出。</p>
              </>
            ) : (
              <>
                <p>这个岗位还没有开始工作</p>
                <p className="dim small">协作推进到它时，这里会实时出现输出。</p>
              </>
            )}
          </div>
        )}
      />
    </div>
  )
}
