/** 移动端协作进度：与桌面端同一张流程图（拖动平移 / 双指缩放 / 点节点看进度）。
 *  图上不放标题与说明：顶栏已有运行标题，状态徽标与图例自己解释，画布直接铺满。 */
import type { CollabFlowEdge, CollabAgentProgress } from '../../lib/collabProgress'
import { CollabFlowMap } from '../../components/CollabFlowMap'

interface Props {
  nodes: CollabAgentProgress[]
  edges: CollabFlowEdge[]
  onOpenNode: (key: string) => void
}

export function MobileCollabProgress({ nodes, edges, onOpenNode }: Props) {
  if (nodes.length === 0) return null

  return (
    <div className="m-collab-progress">
      <CollabFlowMap nodes={nodes} edges={edges} onOpenNode={onOpenNode} />
    </div>
  )
}
