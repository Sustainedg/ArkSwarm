/**
 * 协作进度（全屏）：把这次协作画成一间**等距视角的办公室**，每个岗位上坐着一个会动的小人 + 一张工牌。
 *
 * 工位按层级摆（谁向谁派活）：入口领导者独占最里面一行，往里第二行是它直接派活的人（通常是 CTO），
 * 再往外的两排是并排干活的人——「上面一个、下面一排」的团队结构在图上就能看出来。
 * 工牌上的状态点区分状态（绿=工作中、红=忙碌、黄=等你决定、灰=闲置、绿勾=完成）。
 * 画布支持滚轮缩放与拖动平移；点任一工牌弹出进度实况，也能跳到它在本次协作里的实时对话。
 * 回流（打回 / 返工）不参与分层，单独收在下面的区块里。
 */
import { useEffect, useState } from 'react'
import type { FinishedStep, LiveStep } from '../lib/collabStream'
import { buildFlowLinks, type CollabFlowEdge, type CollabAgentProgress } from '../lib/collabProgress'
import type { CollabStep, CollabTicket } from '../lib/types'
import { CollabOfficeMap } from './CollabOfficeMap'
import { AgentConversationPanel } from './AgentConversationPanel'

interface Props {
  nodes: CollabAgentProgress[]
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

export function CollabProgressBoard({
  nodes, edges, steps, tickets, live, streamed, runTitle, teamName, statusLabel, statusPill, onClose,
}: Props) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (selectedKey) setSelectedKey(null)
      else onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, selectedKey])

  if (nodes.length === 0) return null

  const selectedNode = nodes.find((node) => node.key === selectedKey) ?? null
  const total = nodes.length
  const done = nodes.filter((node) => node.state === 'done').length

  const links = buildFlowLinks(nodes, edges)
  const loops = links.filter((link) => link.isLoop)
  const labels = new Map(nodes.map((node) => [node.key, node.label]))

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

        <div className={`collab-progress-board-body${selectedNode ? ' has-conversation' : ''}`}>
          <div className="collab-progress-board-main">
            <section className="collab-progress-section">
              <div className="collab-progress-section-head">
                <div>
                  <h3>办公室实况</h3>
                  <p>
                    一间等距视角的办公室：滚轮放大、拖动平移，点工位上的工牌看她在忙什么。
                    工牌上的状态点——绿=在线工作、红=忙碌出问题、黄=等你决定、灰=闲置、绿勾=已完成。
                    进度是按岗位状态粗估的，只为一眼看个大概。
                  </p>
                </div>
                <span className="collab-progress-count">{done}/{total} 个岗位已完成</span>
              </div>
              <CollabOfficeMap nodes={nodes} edges={edges} onOpenNode={setSelectedKey} />
            </section>

            {loops.length > 0 && (
              <section className="collab-progress-section collab-progress-flows">
                <div className="collab-progress-section-head">
                  <div>
                    <h3>打回与返工</h3>
                    <p>这些箭头是往回走的：审查不通过就打回上游，返工完再往前走。</p>
                  </div>
                </div>
                <div className="collab-progress-flow-list">
                  {loops.map((link) => (
                    <span className={`collab-progress-flow kind-${link.kind}`} key={link.id}>
                      <span>{labels.get(link.fromKey) ?? link.fromKey}</span>
                      <b>{link.kindLabel}</b>
                      <span>{labels.get(link.toKey) ?? link.toKey}</span>
                    </span>
                  ))}
                </div>
              </section>
            )}
          </div>

          {selectedNode && (
            <aside className="collab-progress-conversation">
              <AgentConversationPanel
                node={selectedNode}
                roster={nodes}
                steps={steps}
                tickets={tickets}
                live={live}
                streamed={streamed}
                onClose={() => setSelectedKey(null)}
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
