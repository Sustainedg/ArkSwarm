/**
 * 一个坐在转椅上的 3D 小人（纯 CSS 画的，无图片、无依赖）：转椅（留在工位）+ 可走位的身体层
 * （气泡 / 脖子 / 头 / 躯干 / 手臂 / 腿 / 杯子）。
 *
 * 只画「人」这一层（连手都不画），桌椅由使用方安排：信息卡的缩略图用 `WorkerAtDesk`（附带整套桌椅、
 * 显示器与按在键盘上的两只手），等距办公室地图则把人直接放到 SVG 画好的桌子后面。
 *
 * 状态差靠**祖先**上的状态类名（`state-pending` 打盹、`state-active` 敲键盘、`state-waiting` 举手、
 * `state-error` 出错、`state-done` 收工）加 `.collab-station-scene` 触发，样式在两端样式表里各一份。
 *
 * 拟人化 v2（`docs/architecture.md` §11）把「人」再拆两层：
 * - **椅子**（`.collab-worker-chair`）是工位的一部分，直挂在 `.collab-worker` 下，**永远不动**——
 *   所以 `walk` / `discuss` 不会把转椅也搬走；
 * - **身体**（`.collab-worker-body`）才是环境行为（L2）与微动作（L3）作用的层。
 *
 * `env` / `seed` 都是**可选**的：不传时（信息卡的缩略图）渲染出的姿态与改造前**等价** ——
 * 没有 `env-*` / `fidget-*` 类名，也没有内联的节拍变量，走 `nth-child` 兜底节拍。
 */
import type { CSSProperties } from 'react'
import { fidgetProfile, type EnvBehavior } from '../lib/collabEnv'
import type { CollabAgentState } from '../lib/collabProgress'

/** 小人头顶的小气泡：一眼看出这个岗位这会儿在忙什么。 */
const STATE_BUBBLE: Record<CollabAgentState, string> = {
  pending: '💤',
  active: '⌨️',
  waiting: '❓',
  error: '💥',
  done: '✅',
}

interface Props {
  state: CollabAgentState
  /** L2 环境行为（由 `CollabOfficeMap` 的调度器给出）；不传 = 只演真实状态。 */
  env?: EnvBehavior | null
  /** 员工稳定种子（`workerSeed(key)`）：决定微动作的种类与节拍；不传 = 用 `nth-child` 兜底节拍。 */
  seed?: number
}

export function CollabWorker({ state, env = null, seed }: Props) {
  // 微动作节拍：同一个人刷新后 / 两端都是同一套（种子由员工 key 派生，不是随机数）。
  const fidget = seed === undefined ? null : fidgetProfile(seed)
  const style = fidget
    ? ({
      '--fidget-delay': `${fidget.delayMs}ms`,
      '--fidget-cycle': `${fidget.cycleMs}ms`,
    } as CSSProperties)
    : undefined

  return (
    <span className="collab-worker" style={style} data-env={env ?? undefined}>
      {/* 转椅：工位的一部分，环境行为的走位不推它 */}
      <span className="collab-worker-chair" />
      {/* 身体：气泡 / 脖子 / 头 / 躯干 / 腿 / 手臂 / 杯子都在这一层，走位只推这一层 */}
      <span className="collab-worker-body">
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
        {/* 腿：只有 `fidget-leg`（抖腿）时才现形 */}
        <span className="collab-worker-leg left" />
        <span className="collab-worker-leg right" />
        <span className="collab-worker-arm left" />
        <span className="collab-worker-arm right" />
        {/* 杯子：只有 `env-rest`（休息喝咖啡）时才端在手里 */}
        <span className="collab-worker-cup" />
      </span>
    </span>
  )
}
