/**
 * 工单看板抽屉：左边是「父工单 → 子工单」的树，右边是选中工单的细节。
 *
 * 工单是角色之间**唯一**的交接物，所以这张抽屉就是「谁把什么交给了谁」的全貌：
 * 工单字段（目标 / 要求 / 验收 / 约束）、产出与文件、审查意见与返工轮次，
 * 以及只读节点运行结束后的越权审计（`step.audit`）。
 *
 * 数据全部来自已经拿到的运行详情（`CollabRunDetail`），不额外发请求。
 */
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import type { CollabStep, CollabTicket } from '../lib/types'

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

const BRIEF_FIELDS: { key: string; label: string }[] = [
  { key: 'goal', label: '目标' },
  { key: 'requirements', label: '要求' },
  { key: 'acceptance', label: '验收标准' },
  { key: 'constraints', label: '约束' },
  { key: 'deliverables', label: '交付物' },
]

interface Props {
  tickets: CollabTicket[]
  steps: CollabStep[]
  /** 把节点 key 翻成人看的角色名 */
  nodeLabel: (key: string) => string
  onClose: () => void
}

export function TicketBoard({ tickets, steps, nodeLabel, onClose }: Props) {
  const ordered = useMemo(
    () => [...tickets].sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0)),
    [tickets],
  )
  const children = useMemo(() => {
    const ids = new Set(ordered.map((ticket) => ticket.id))
    const map = new Map<string, CollabTicket[]>()
    for (const ticket of ordered) {
      // 父工单不在这批数据里（理论上不该发生）时当成根，保证每张工单都看得见、不被静默吞掉
      const parent = ticket.parent_ticket_id && ids.has(ticket.parent_ticket_id) ? ticket.parent_ticket_id : null
      const key = parent ?? '__root__'
      map.set(key, [...(map.get(key) ?? []), ticket])
    }
    return map
  }, [ordered])

  // 默认挑「最需要你介入」的那张：卡住的 → 返工中 → 待审查 → 根工单
  const preferred =
    ordered.find((ticket) => ticket.state === 'blocked') ??
    ordered.find((ticket) => ticket.state === 'rework') ??
    ordered.find((ticket) => ticket.state === 'review') ??
    ordered.find((ticket) => !ticket.parent_ticket_id) ??
    ordered[0]
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const current = ordered.find((ticket) => ticket.id === (selectedId ?? preferred?.id)) ?? preferred

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const renderBranch = (parentId: string | null, depth: number): ReactNode[] => {
    if (depth > 8) return []
    return (children.get(parentId ?? '__root__') ?? []).map((ticket) => [
      <button
        key={ticket.id}
        className={`collab-tree-item${ticket.id === current?.id ? ' active' : ''}`}
        style={{ paddingLeft: 10 + depth * 16 }}
        onClick={() => setSelectedId(ticket.id)}
      >
        <span className={`pill ${TICKET_STATE[ticket.state]?.pill ?? ''}`}>
          {TICKET_STATE[ticket.state]?.label ?? ticket.state}
        </span>
        <span className="collab-tree-title">{ticket.title}</span>
        {ticket.round > 0 && <span className="collab-tree-round">返工 {ticket.round}</span>}
      </button>,
      ...renderBranch(ticket.id, depth + 1),
    ])
  }

  return (
    <div className="collab-drawer-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="collab-drawer" role="dialog" aria-modal="true" aria-label="工单看板">
        <div className="collab-drawer-head">
          <div>
            <div className="collab-inspector-title">工单看板</div>
            <div className="dim small">共 {tickets.length} 张 · 点一张看细节（角色之间只传工单）</div>
          </div>
          <button className="btn ghost sm" style={{ marginLeft: 'auto' }} onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>
        <div className="collab-drawer-body">
          <div className="collab-tree">
            {ordered.length === 0 ? (
              <p className="dim small">这次协作还没有工单。</p>
            ) : (
              renderBranch(null, 0)
            )}
          </div>
          <div className="collab-detail">
            {current ? (
              <TicketDetail
                ticket={current}
                steps={steps.filter((step) => step.ticket_id === current.id)}
                nodeLabel={nodeLabel}
              />
            ) : (
              <p className="dim small">入口角色还没把活派出去。</p>
            )}
          </div>
        </div>
      </aside>
    </div>
  )
}

function TicketDetail({
  ticket, steps, nodeLabel,
}: {
  ticket: CollabTicket
  steps: CollabStep[]
  nodeLabel: (key: string) => string
}) {
  const artifacts = asArtifacts(ticket.result?.artifacts)
  const summary = typeof ticket.result?.summary === 'string' ? ticket.result.summary : ''

  return (
    <>
      <div className="collab-detail-head">
        <span className={`pill ${TICKET_STATE[ticket.state]?.pill ?? ''}`}>
          {TICKET_STATE[ticket.state]?.label ?? ticket.state}
        </span>
        <span className="collab-detail-title">{ticket.title}</span>
        <span className="dim small">{`返工 ${ticket.round} / ${ticket.max_rounds || '—'} 轮`}</span>
      </div>
      <div className="dim small">
        现在在 {nodeLabel(ticket.holder_key) || ticket.holder_key} 手里
        {ticket.owner_key && ` · 汇总人 ${nodeLabel(ticket.owner_key) || ticket.owner_key}`}
        {ticket.submitter_key && ` · 提交人 ${nodeLabel(ticket.submitter_key) || ticket.submitter_key}`}
      </div>

      <div className="collab-detail-grid">
        {BRIEF_FIELDS.map((field) => {
          const raw = (ticket.brief as Record<string, unknown> | undefined)?.[field.key]
          const lines = asStringList(raw)
          if (lines.length === 0) return null
          return (
            <div key={field.key} className="collab-detail-field">
              <div className="collab-detail-label">{field.label}</div>
              <ul className="collab-detail-list">
                {lines.map((line, index) => (
                  <li key={index}>{line}</li>
                ))}
              </ul>
            </div>
          )
        })}
      </div>

      {(summary || artifacts.length > 0) && (
        <div className="collab-detail-field">
          <div className="collab-detail-label">产出</div>
          {summary && <p style={{ margin: 0 }}>{summary}</p>}
          {artifacts.length > 0 && (
            <ul className="collab-detail-list">
              {artifacts.map((artifact) => (
                <li key={artifact.key} className="mono">
                  {artifact.label}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {ticket.review_comment && (
        <div className={`banner ${ticket.verdict === 'pass' ? 'info' : 'warn'}`}>
          审查意见：{ticket.review_comment}
        </div>
      )}

      <div className="collab-detail-field">
        <div className="collab-detail-label">这一张工单上发生的事</div>
        {steps.filter((step) => step.role_key !== 'user').length === 0 ? (
          <p className="dim small" style={{ margin: 0 }}>还没有角色处理过它。</p>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            {steps
              .filter((step) => step.role_key !== 'user')
              .map((step) => (
                <div key={step.id} className="collab-detail-step">
                  <div className="row" style={{ gap: 6 }}>
                    <span className="collab-detail-step-role">{nodeLabel(step.role_key) || step.role_key}</span>
                    <span className="pill">{step.action || step.status}</span>
                    <span className="dim small">{usageLine(step.usage)}</span>
                  </div>
                  {step.gist && <div className="dim small">{step.gist}</div>}
                  {step.error && <div className="banner err">{step.error}</div>}
                  <AuditBlock audit={step.audit} />
                </div>
              ))}
          </div>
        )}
      </div>
    </>
  )
}

/** 只读节点跑完后对共享工作目录的越权检查（`execution.finish_audit`）。 */
function AuditBlock({ audit }: { audit: Record<string, unknown> }) {
  if (!audit || audit.checked !== true) return null
  const added = asStringList(audit.added)
  const modified = asStringList(audit.modified)
  const removed = asStringList(audit.removed)
  const count = typeof audit.count === 'number' ? audit.count : added.length + modified.length + removed.length

  if (count === 0) {
    return <div className="dim small">工作目录未被改动 ✅</div>
  }
  return (
    <div className="banner err">
      <div>只读节点却动了工作目录（{count} 个文件），已记入审计：</div>
      {(
        [
          ['新增', added],
          ['修改', modified],
          ['删除', removed],
        ] as const
      ).map(([label, files]) =>
        files.length === 0 ? null : (
          <div key={label} className="small">
            {label}：<span className="mono">{files.join('、')}</span>
          </div>
        ),
      )}
    </div>
  )
}

function usageLine(usage: Record<string, number>): string {
  const total = Object.values(usage ?? {}).reduce((sum, value) => sum + (typeof value === 'number' ? value : 0), 0)
  return total > 0 ? `${total.toLocaleString()} tok` : ''
}

/**
 * 产出物：协议允许写字符串，后端会把它规整成 `{kind: 'file', path}`；
 * 这里两种都要认，不能让界面出现 `[object Object]`。
 */
function asArtifacts(value: unknown): { key: string; label: string }[] {
  if (!Array.isArray(value)) return []
  const items: { key: string; label: string }[] = []
  value.forEach((item, index) => {
    if (typeof item === 'string' && item.trim()) {
      items.push({ key: `${index}-${item}`, label: item })
      return
    }
    if (item && typeof item === 'object') {
      const record = item as Record<string, unknown>
      const path = record.path ?? record.name ?? record.url
      if (typeof path === 'string' && path.trim()) {
        const kind = typeof record.kind === 'string' ? record.kind : ''
        items.push({ key: `${index}-${path}`, label: kind && kind !== 'file' ? `${path}（${kind}）` : path })
      }
    }
  })
  return items
}

function asStringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean)
  if (typeof value === 'string' && value.trim()) return [value]
  return []
}
