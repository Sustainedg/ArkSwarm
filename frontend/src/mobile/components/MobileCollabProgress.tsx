/** 移动端协作进度：横向滑动查看岗位状态，点击进入该岗位的内部实时会话。 */
import { Fragment } from 'react'
import type { CollabFlowEdge, CollabRoleProgress } from '../../lib/collabProgress'

interface Props {
  nodes: CollabRoleProgress[]
  edges: CollabFlowEdge[]
  onOpenRole: (key: string) => void
}

export function MobileCollabProgress({ nodes, edges, onOpenRole }: Props) {
  if (nodes.length === 0) return null
  return (
    <div className="m-collab-progress">
      <div className="m-collab-progress-head">
        <span>协作进度</span>
        <span className="dim">点岗位查看实时对话</span>
      </div>
      <div className="m-collab-progress-track">
        {nodes.map((node, index) => (
          <Fragment key={node.key}>
            {index > 0 && <span className="m-collab-progress-arrow">→</span>}
            <button
              className={`m-collab-progress-node state-${node.state}${node.isLive ? ' live' : ''}`}
              onClick={() => onOpenRole(node.key)}
            >
              <span className="m-collab-progress-node-head">
                <span className="avatar">{node.avatar}</span>
                <span className="m-collab-progress-label">{node.label}</span>
                {node.isEntry && <span className="m-badge">领导者</span>}
              </span>
              <span className="m-collab-progress-state">{node.stateLabel}</span>
              <span className="m-collab-progress-meta">
                {node.stepCount > 0 ? `${node.stepCount} 步` : '未参与'}
                {node.lastAction ? ` · ${node.lastAction}` : ''}
              </span>
            </button>
          </Fragment>
        ))}
      </div>
      {edges.length > 0 && (
        <div className="m-collab-progress-links">
          {edges.map((edge) => (
            <span className={`m-collab-progress-link kind-${edge.kind}`} key={edge.id}>
              {edge.from}
              <b>{edge.kindLabel}</b>
              {edge.to}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
