/**
 * 「对话有改动」的广播（新建 / 重命名 / 删除）。
 *
 * 会改对话的是对话页（发送、删除），画面却画在左侧栏的列表里 —— 两者不在同一棵组件树上，
 * 用一个自定义事件对齐：改完喊一声，列表立刻重新拉一次，不用干等下一次轮询（最多 8 秒）。
 */
import { useEffect } from 'react'

const EVENT = 'arkswarm:conversations-changed'

/** 广播一次「对话列表变了」。 */
export function notifyConversationsChanged(): void {
  window.dispatchEvent(new Event(EVENT))
}

/** 订阅广播（`handler` 请用 `useCallback` 包一层，避免每次渲染重订阅）。 */
export function useConversationsChanged(handler: () => void): void {
  useEffect(() => {
    window.addEventListener(EVENT, handler)
    return () => window.removeEventListener(EVENT, handler)
  }, [handler])
}
