/** 移动端协作进度：与桌面端同一间等距办公室（双指缩放 / 单指拖动 / 点工牌看进度）。 */
import { buildFlowLinks, type CollabFlowEdge, type CollabRoleProgress } from '../../lib/collabProgress'
import { CollabOfficeMap } from '../../components/CollabOfficeMap'

interface Props {
  nodes: CollabRoleProgress[]
  edges: CollabFlowEdge[]
  onOpenRole: (key: string) => void
}

export function MobileCollabProgress({ nodes, edges, onOpenRole }: Props) {
  if (nodes.length === 0) return null
  const loops = buildFlowLinks(nodes, edges).filter((link) => link.isLoop)

  return (
    <div className="m-collab-progress">
      <div className="m-collab-progress-head">
        <span>协作进度</span>
        <span className="dim">双指缩放 · 拖动平移 · 点工牌看详情</span>
      </div>
      <CollabOfficeMap nodes={nodes} edges={edges} onOpenRole={onOpenRole} initialView="desks" />
      {loops.length > 0 && (
        <div className="m-collab-progress-links">
          {loops.map((link) => (
            <span className={`m-collab-progress-link kind-${link.kind}`} key={link.id}>
              {nodes.find((node) => node.key === link.fromKey)?.label ?? link.fromKey}
              <b>↩ {link.kindLabel}</b>
              {nodes.find((node) => node.key === link.toKey)?.label ?? link.toKey}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
