/**
 * 协作工位卡上的一个小人：坐在电脑前敲代码的 3D 小人 + 一整套桌椅、显示器、键盘、咖啡杯。
 *
 * 人本身来自共享组件 `CollabWorker`（等距办公室地图也用同一份结构）；这里补上它面前的那张桌子。
 * 外观尺寸由各自的样式表决定（`index.css` / `mobile.css`），状态差靠外层 `state-*` 类名。
 */
import type { CollabAgentState } from '../lib/collabProgress'
import { CollabWorker } from './CollabWorker'

export function WorkerAtDesk({ state, live = false }: { state: CollabAgentState; live?: boolean }) {
  return (
    <span
      className={`collab-station-scene state-${state}${live ? ' live' : ''}`}
      aria-hidden="true"
    >
      <span className="collab-desk-ground" />
      <CollabWorker state={state} />
      <span className="collab-workdesk">
        <span className="collab-workdesk-top" />
        <span className="collab-workdesk-lip" />
        <span className="collab-workdesk-apron" />
        <span className="collab-workdesk-leg left" />
        <span className="collab-workdesk-leg right" />
        <span className="collab-workdesk-mug" />
        <span className="collab-workdesk-monitor">
          <span className="collab-workdesk-code"><i /><i /><i /><i /></span>
          <span className="collab-workdesk-caret" />
        </span>
        <span className="collab-workdesk-keyboard">
          <i /><i /><i /><i /><i /><i /><i /><i /><i /><i />
        </span>
        <span className="collab-hand left" />
        <span className="collab-hand right" />
      </span>
    </span>
  )
}
