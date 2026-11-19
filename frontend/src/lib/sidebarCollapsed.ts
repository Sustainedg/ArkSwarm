/**
 * 侧边栏折叠状态：App 外壳与对话页顶栏**两处入口**共用同一份真值。
 *
 * 真值存在 localStorage（折叠是这台设备的个人偏好），两处入口通过一个自定义事件
 * 互相通知 —— 没有它的话，顶栏按钮改了状态，外壳的 `collapsed` 类还蒙在鼓里。
 * （`storage` 事件只在**其它标签页**触发，同一页内必须自己喊一嗓子。）
 */

/** 存储键（值 `collapsed` / `open`，与 `lib/theme.ts` 一样是本机偏好）。 */
const SIDEBAR_KEY = 'arkswarm.sidebar'
/** 同页通知用的事件名（附带的 detail 是最新值）。 */
const SIDEBAR_EVENT = 'arkswarm:sidebar-toggle'

export function readSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === 'collapsed'
  } catch {
    /* 隐私模式下 localStorage 可能不可用：默认展开 */
    return false
  }
}

/** 折叠 / 展开（写入 localStorage 并广播给同页的其它入口）。 */
export function toggleSidebarCollapsed(): boolean {
  const next = !readSidebarCollapsed()
  try {
    localStorage.setItem(SIDEBAR_KEY, next ? 'collapsed' : 'open')
  } catch {
    /* 存不进去也不影响本次会话 */
  }
  window.dispatchEvent(new CustomEvent(SIDEBAR_EVENT, { detail: next }))
  return next
}

/**
 * 订阅折叠变化（`useSyncExternalStore` 的 subscribe 形态）：
 * 同页自定义事件 + 跨标签页 storage 事件一起听，谁改的都能跟上。
 */
export function subscribeSidebarCollapsed(onChange: () => void): () => void {
  window.addEventListener(SIDEBAR_EVENT, onChange)
  window.addEventListener('storage', onChange)
  return () => {
    window.removeEventListener(SIDEBAR_EVENT, onChange)
    window.removeEventListener('storage', onChange)
  }
}
