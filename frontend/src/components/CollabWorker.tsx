/**
 * 一个坐在转椅上的 3D 小人（纯 CSS 画的，无图片、无依赖）：转椅 + 头（五官 / 头发）+ 身体 + 两条手臂。
 *
 * 只画「人」这一层（连手都不画），桌椅由使用方安排：信息卡的缩略图用 `WorkerAtDesk`（附带整套桌椅、
 * 显示器与按在键盘上的两只手），等距办公室地图则把人直接放到 SVG 画好的桌子后面。
 *
 * 状态差靠**祖先**上的状态类名（`state-pending` 打盹、`state-active` 敲键盘、`state-waiting` 举手、
 * `state-error` 出错、`state-done` 收工）加 `.collab-station-scene` 触发，样式在两端样式表里各一份。
 */
import type { CollabRoleState } from '../lib/collabProgress'

/** 小人头顶的小气泡：一眼看出这个岗位这会儿在忙什么。 */
const STATE_BUBBLE: Record<CollabRoleState, string> = {
  pending: '💤',
  active: '⌨️',
  waiting: '❓',
  error: '💥',
  done: '✅',
}

export function CollabWorker({ state }: { state: CollabRoleState }) {
  return (
    <span className="collab-worker">
      <span className="collab-worker-chair" />
      <span className="collab-worker-bubble">{STATE_BUBBLE[state]}</span>
      <span className="collab-worker-neck" />
      <span className="collab-worker-head">
        <span className="collab-worker-ear left" />
        <span className="collab-worker-ear right" />
        <span className="collab-worker-hair" />
        <span className="collab-worker-eye left" />
        <span className="collab-worker-eye right" />
        <span className="collab-worker-cheek left" />
        <span className="collab-worker-cheek right" />
        <span className="collab-worker-mouth" />
      </span>
      <span className="collab-worker-torso" />
      <span className="collab-worker-arm left" />
      <span className="collab-worker-arm right" />
    </span>
  )
}
