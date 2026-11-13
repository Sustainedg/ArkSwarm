/**
 * 「已读回执」：用户在看某条对话时，把这个状态告诉后端，左侧列表据此决定还亮不亮未读点。
 *
 * 规则只有一条：**最后一条是已完成的助手回复**才算读过。
 * - 正在流式生成（``status === 'streaming'``）时不标已读 —— 否则用户看到一半切走，
 *   回复完成后也不会提示未读；
 * - 页面在后台（切走标签页 / 锁屏）时不标 —— 回来了下一轮轮询会补上。
 *
 * 同一条消息只发一次请求（``marked`` / ``pending`` 两道去重）：聊天页每 3 秒轮询一次，
 * 不去重就会一直打 ``POST /read``。
 */
import { useCallback, useRef } from 'react'
import { api } from './api'
import { notifyConversationsChanged } from './conversations'
import type { ConversationDetail } from './types'

export function useMarkConversationRead(): (detail: ConversationDetail) => void {
  /** 已经标记成功的那条消息（``对话 id:消息 id``） */
  const marked = useRef<string | null>(null)
  /** 正在请求中的那条：避免轮询在请求返回前又发一遍 */
  const pending = useRef<string | null>(null)

  return useCallback((detail: ConversationDetail) => {
    const last = detail.messages[detail.messages.length - 1]
    if (!last || last.role !== 'assistant' || last.status === 'streaming') return
    if (document.visibilityState === 'hidden') return

    const key = `${detail.conversation.id}:${last.id}`
    if (marked.current === key || pending.current === key) return

    pending.current = key
    api
      .markConversationRead(detail.conversation.id)
      .then(() => {
        marked.current = key
        // 列表立刻把未读点撤掉，不用等下一次轮询。
        notifyConversationsChanged()
      })
      .catch(() => {
        // 只是提示状态，失败就让下一轮轮询再试，不打扰用户。
      })
      .finally(() => {
        if (pending.current === key) pending.current = null
      })
  }, [])
}
