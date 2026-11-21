/**
 * 「对话有改动」的广播（新建 / 重命名 / 删除）。
 *
 * 会改对话的是对话页（发送、删除），画面却画在左侧栏的列表里 —— 两者不在同一棵组件树上，
 * 用一个自定义事件对齐：改完喊一声，列表立刻重新拉一次，不用干等下一次轮询（最多 8 秒）。
 */
import { useEffect } from 'react'
import { draftOwnerIds } from './composerDraft'
import type { Conversation } from './types'

const EVENT = 'coda:conversations-changed'

/** 兜底标题（与后端 `DEFAULT_TITLE` 同值）：标题还是它 = 用户从没发过第一条消息。 */
export const NEW_CONVERSATION_TITLE = '新对话'

/**
 * 侧栏排序兜底：按「最近活动」倒序。
 *
 * 后端已经按同样的口径排过（`updated_at` 与最后一条消息时间取较晚者），这里再排一次是
 * 为了两件事：① 本地乐观插入（新建对话 / 刚发完消息）后不必等下一轮接口就能立刻到顶；
 * ② 万一某个部署的后端还在返回旧顺序，界面也不会跟着错。
 */
export function sortConversationsByRecent(items: Conversation[]): Conversation[] {
  return [...items].sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))
}

/** 广播一次「对话列表变了」。 */
export function notifyConversationsChanged(): void {
  window.dispatchEvent(new Event(EVENT))
}

/**
 * 从列表里挑一条「从未被使用」的空对话：连点「新建对话」时直接跳回它，不再堆出一排
 * 「新对话」。判定标准（须同时满足）：
 *
 * 1. 一条消息都没发过（`message_count` 为 0）；
 * 2. 标题还是兜底值「新对话」——发第一条消息时后端就把标题换成首行了，所以标题
 *    还没动过 = 从没说过话；用户手动改过名的也跳过，那是人家起了名字要留着的；
 * 3. 输入框里**没有**草稿（本地记录）——空对话但打了字，说明用户正在这条里酝酿，
 *    这时再点新建就得真开一条，别把人家打到一半的字「藏」进旧对话里。
 *
 * 多条都符合时取列表里的第一条（后端按最近活动排过序）。没有符合的返回 `null`。
 */
export function findUnusedConversation(items: Conversation[]): Conversation | null {
  const drafted = draftOwnerIds()
  for (const conversation of items) {
    if (conversation.message_count > 0) continue
    if (conversation.title !== NEW_CONVERSATION_TITLE) continue
    if (drafted.has(conversation.id)) continue
    return conversation
  }
  return null
}

/** 订阅广播（`handler` 请用 `useCallback` 包一层，避免每次渲染重订阅）。 */
export function useConversationsChanged(handler: () => void): void {
  useEffect(() => {
    window.addEventListener(EVENT, handler)
    return () => window.removeEventListener(EVENT, handler)
  }, [handler])
}
