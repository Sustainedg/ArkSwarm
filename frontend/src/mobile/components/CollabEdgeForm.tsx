import { useState } from 'react'
import { EDGE_KINDS } from '../../lib/collabDraft'
import type { CollabEdge, CollabEdgeKind, CollabNode } from '../../lib/types'


/**
 * 协作「连线」表单（移动端）：用「从谁到谁 + 类型」的下拉选择替代画布的拖拽连线
 * （画布只在桌面端使用）。只编辑这一条边。
 */
export function CollabEdgeForm({
  edge,
  nodes,
  defaultFrom,
  onSave,
  onDelete,
}: {
  edge?: CollabEdge
  nodes: CollabNode[]
  defaultFrom: string
  onSave: (next: Omit<CollabEdge, 'id'>) => void
  onDelete: () => void
}) {
  const [form, setForm] = useState<Omit<CollabEdge, 'id'>>(
    edge
      ? {
          from_key: edge.from_key, to_key: edge.to_key, kind: edge.kind, label: edge.label,
          max_rounds: edge.max_rounds, from_side: edge.from_side ?? null, to_side: edge.to_side ?? null,
        }
      : {
          from_key: defaultFrom,
          to_key: nodes.find((item) => item.key !== defaultFrom)?.key ?? '',
          kind: 'assign',
          label: '',
          max_rounds: 0,
          from_side: null,
          to_side: null,
        },
  )
  const [error, setError] = useState<string | null>(null)
  const set = (next: Partial<CollabEdge>) => setForm((prev) => ({ ...prev, ...next }))

  const submit = () => {
    if (!form.from_key || !form.to_key) return setError('请选择起点与终点岗位')
    if (form.from_key === form.to_key) return setError('连线不能首尾是同一个岗位')
    onSave(form)
  }

  return (
    <>
      {error && <div className="banner err">{error}</div>}

      <div className="field">
        <label className="field-label">从</label>
        <select className="select" value={form.from_key} onChange={(event) => set({ from_key: event.target.value })}>
          {nodes.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label || item.key}（{item.key}）
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="field-label">到</label>
        <select className="select" value={form.to_key} onChange={(event) => set({ to_key: event.target.value })}>
          {nodes.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label || item.key}（{item.key}）
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="field-label">类型</label>
        <select
          className="select"
          value={form.kind}
          onChange={(event) => set({ kind: event.target.value as CollabEdgeKind })}
        >
          {EDGE_KINDS.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <span className="field-help">{EDGE_KINDS.find((item) => item.value === form.kind)?.hint}</span>
      </div>

      {form.kind === 'rework' && (
        <>
          <div className="field-help">
            这条线本身已经指定打回目标，运行时不需要额外的 rework_to。只有当同一审查者画了多条
            rework 出边时，审查者才需要在工单里写明 rework_to 来选择退路。
          </div>
          <div className="field">
            <label className="field-label">这条边的返工上限</label>
            <input
              className="input mono"
              type="number"
              min={0}
              value={form.max_rounds}
              onChange={(event) => set({ max_rounds: Math.max(0, Number(event.target.value) || 0) })}
            />
            <span className="field-help">0 表示按团队默认；实际取「边 / 节点 / 团队」三者最小。</span>
          </div>
        </>
      )}

      <div className="field">
        <label className="field-label">备注（可选）</label>
        <input
          className="input"
          value={form.label}
          onChange={(event) => set({ label: event.target.value })}
          placeholder="例如：审查通过后交给实施者收尾"
        />
      </div>

      <div className="row" style={{ gap: 8 }}>
        {edge && (
          <button className="btn danger" onClick={onDelete}>
            删除连线
          </button>
        )}
        <button className="btn primary" style={{ flex: 1 }} onClick={submit}>
          {edge ? '保存连线' : '添加连线'}
        </button>
      </div>
    </>
  )
}
