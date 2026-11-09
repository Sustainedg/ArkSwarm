/**
 * 协作编排画布：手写的「拖节点 + 连有向边」，不引入任何图库（铁律 12）。
 *
 * 交互刻意保持简单（评审结论）：节点是 `position: absolute` 的 div，连线是节点**右侧连接点**
 * 拖到另一个节点松手后画出的**三次贝塞尔曲线**；不做折线避让、不做自动布局。
 */
import { useEffect, useRef, useState } from 'react'
import { addEdgeToGraph } from '../lib/collabDraft'
import type { CollabEdge, CollabEdgeKind, CollabIssue, CollabNode, Role } from '../lib/types'

/** 节点卡片的固定尺寸：连线端点按它计算（M2 只做等宽等高卡片） */
const NODE_W = 190
const NODE_H = 92
const CANVAS_W = 1400
const CANVAS_H = 900

export interface CollabSelection {
  kind: 'node' | 'edge'
  /** 节点用 key，边用 id */
  id: string
}

interface Props {
  nodes: CollabNode[]
  edges: CollabEdge[]
  roles: Role[]
  entryKey?: string | null
  selected: CollabSelection | null
  issues: CollabIssue[]
  onChange: (graph: { nodes: CollabNode[]; edges: CollabEdge[] }) => void
  onSelect: (selection: CollabSelection | null) => void
  /** 把左栏的某个角色拖到画布上的落点（坐标为画布内坐标） */
  onDropRole?: (roleId: string, at: { x: number; y: number }) => void
}

type DragState =
  | { kind: 'node'; key: string; offsetX: number; offsetY: number }
  | { kind: 'link'; fromKey: string }

const DUTY_LABEL: Record<string, string> = {
  coordinator: '协调者',
  engineer: '实施者',
  reviewer: '审查者',
}

function edgeColor(kind: CollabEdgeKind): string {
  if (kind === 'review') return '#6ee7d1'
  if (kind === 'rework') return '#fbbf24'
  return '#8b86b8'
}

function isTypingTarget(target: EventTarget | null): boolean {
  const tag = (target as HTMLElement | null)?.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
}

/** 端点坐标：从源节点右侧中点出发，到目标节点左侧中点 */
function anchorRight(node: CollabNode) {
  return { x: node.x + NODE_W, y: node.y + NODE_H / 2 }
}
function anchorLeft(node: CollabNode) {
  return { x: node.x, y: node.y + NODE_H / 2 }
}
function curve(from: { x: number; y: number }, to: { x: number; y: number }): string {
  const bend = Math.max(36, Math.abs(to.x - from.x) / 2)
  return `M ${from.x} ${from.y} C ${from.x + bend} ${from.y}, ${to.x - bend} ${to.y}, ${to.x} ${to.y}`
}

export function CollabCanvas({ nodes, edges, roles, entryKey, selected, issues, onChange, onSelect, onDropRole }: Props) {
  const innerRef = useRef<HTMLDivElement | null>(null)
  const [drag, setDrag] = useState<DragState | null>(null)
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null)

  const byKey = new Map(nodes.map((node) => [node.key, node]))
  const roleById = new Map(roles.map((role) => [role.id, role]))
  const nodeErrorKeys = new Set(issues.filter((issue) => issue.node_key).map((issue) => issue.node_key))
  const edgeErrorIds = new Set(issues.filter((issue) => issue.edge_id).map((issue) => issue.edge_id))

  const toCanvas = (clientX: number, clientY: number) => {
    const rect = innerRef.current?.getBoundingClientRect()
    return { x: clientX - (rect?.left ?? 0), y: clientY - (rect?.top ?? 0) }
  }

  // 拖动 / 连线期间在 window 上监听，鼠标移出画布也不会卡住
  useEffect(() => {
    if (!drag) return
    const onMove = (event: PointerEvent) => {
      const point = toCanvas(event.clientX, event.clientY)
      if (drag.kind === 'node') {
        const next = nodes.map((node) =>
          node.key === drag.key
            ? { ...node, x: Math.max(0, Math.round(point.x - drag.offsetX)), y: Math.max(0, Math.round(point.y - drag.offsetY)) }
            : node,
        )
        onChange({ nodes: next, edges })
      } else {
        setCursor(point)
      }
    }
    const onUp = (event: PointerEvent) => {
      if (drag.kind === 'link') {
        const target = document
          .elementFromPoint(event.clientX, event.clientY)
          ?.closest('[data-node-key]') as HTMLElement | null
        const targetKey = target?.dataset.nodeKey
        if (targetKey && targetKey !== drag.fromKey) {
          onChange({ nodes, edges: addEdgeToGraph(nodes, edges, drag.fromKey, targetKey) })
        }
      }
      setDrag(null)
      setCursor(null)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [drag, nodes, edges, onChange, byKey])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') return
      if (!selected || isTypingTarget(event.target)) return
      event.preventDefault()
      if (selected.kind === 'node') {
        onChange({
          nodes: nodes.filter((node) => node.key !== selected.id),
          edges: edges.filter((edge) => edge.from_key !== selected.id && edge.to_key !== selected.id),
        })
      } else {
        onChange({ nodes, edges: edges.filter((edge) => edge.id !== selected.id) })
      }
      onSelect(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selected, nodes, edges, onChange, onSelect])

  const linkFrom = drag?.kind === 'link' ? byKey.get(drag.fromKey) : undefined

  return (
    <div className="collab-canvas">
      <div
        className="collab-canvas-inner"
        ref={innerRef}
        style={{ width: CANVAS_W, height: CANVAS_H }}
        onPointerDown={(event) => {
          if (event.target === event.currentTarget) onSelect(null)
        }}
        onDragOver={(event) => {
          if (onDropRole) event.preventDefault()
        }}
        onDrop={(event) => {
          const roleId = event.dataTransfer.getData('text/collab-role')
          if (!roleId || !onDropRole) return
          event.preventDefault()
          const point = toCanvas(event.clientX, event.clientY)
          onDropRole(roleId, { x: Math.max(0, point.x - NODE_W / 2), y: Math.max(0, point.y - 24) })
        }}
      >
        <svg className="collab-links" width={CANVAS_W} height={CANVAS_H}>
          <defs>
            {(['assign', 'review', 'rework'] as CollabEdgeKind[]).map((shape) => (
              <marker
                key={shape}
                id={`collab-arrow-${shape}`}
                viewBox="0 0 10 10"
                refX="9"
                refY="5"
                markerWidth="7"
                markerHeight="7"
                orient="auto-start-reverse"
              >
                <path d="M 0 0 L 10 5 L 0 10 z" fill={edgeColor(shape)} />
              </marker>
            ))}
          </defs>
          {edges.map((edge) => {
            const from = byKey.get(edge.from_key)
            const to = byKey.get(edge.to_key)
            if (!from || !to) return null
            const path = curve(anchorRight(from), anchorLeft(to))
            const active = selected?.kind === 'edge' && selected.id === edge.id
            return (
              <g key={edge.id}>
                <path
                  d={path}
                  className="collab-link-hit"
                  onPointerDown={(event) => {
                    event.stopPropagation()
                    onSelect({ kind: 'edge', id: edge.id })
                  }}
                />
                <path
                  d={path}
                  className={`collab-link${active ? ' active' : ''}${edgeErrorIds.has(edge.id) ? ' err' : ''}`}
                  stroke={edgeColor(edge.kind)}
                  strokeDasharray={edge.kind === 'rework' ? '7 5' : undefined}
                  markerEnd={`url(#collab-arrow-${edge.kind})`}
                />
                {edge.label && (
                  <text className="collab-link-label" x={(anchorRight(from).x + anchorLeft(to).x) / 2} y={(anchorRight(from).y + anchorLeft(to).y) / 2 - 8}>
                    {edge.label}
                  </text>
                )}
              </g>
            )
          })}
          {linkFrom && cursor && (
            <path d={curve(anchorRight(linkFrom), cursor)} className="collab-link preview" />
          )}
        </svg>

        {nodes.map((node) => {
          const role = roleById.get(node.role_id)
          const active = selected?.kind === 'node' && selected.id === node.key
          return (
            <div
              key={node.key}
              data-node-key={node.key}
              className={`collab-node${active ? ' active' : ''}${nodeErrorKeys.has(node.key) ? ' err' : ''}`}
              style={{ left: node.x, top: node.y, width: NODE_W, height: NODE_H }}
              onPointerDown={(event) => {
                event.stopPropagation()
                onSelect({ kind: 'node', id: node.key })
                const point = toCanvas(event.clientX, event.clientY)
                setDrag({ kind: 'node', key: node.key, offsetX: point.x - node.x, offsetY: point.y - node.y })
              }}
            >
              <div className="collab-node-head">
                <span className="collab-node-avatar">{role?.avatar ?? '🤖'}</span>
                <span className="collab-node-label">{node.label || role?.name || node.key}</span>
                {entryKey === node.key && <span className="collab-node-entry" title="入口节点（唯一能与用户对话）">★</span>}
              </div>
              <div className="collab-node-meta">
                <span className={`pill ${node.duty === 'reviewer' ? 'ok' : node.duty === 'coordinator' ? 'brand' : ''}`}>
                  {DUTY_LABEL[node.duty] ?? node.duty}
                </span>
                <span className="mono-sm dim">{node.key}</span>
              </div>
              <div className="collab-node-desc">{node.capability_scope || role?.name || '（未设置能力边界）'}</div>
              <span
                className="collab-port collab-port-out"
                title="拖到另一个节点即可连线"
                onPointerDown={(event) => {
                  event.stopPropagation()
                  event.preventDefault()
                  onSelect({ kind: 'node', id: node.key })
                  setDrag({ kind: 'link', fromKey: node.key })
                  setCursor(toCanvas(event.clientX, event.clientY))
                }}
              />
              <span className="collab-port collab-port-in" />
            </div>
          )
        })}
      </div>
    </div>
  )
}

