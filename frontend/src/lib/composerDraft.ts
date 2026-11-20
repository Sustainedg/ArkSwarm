/**
 * 每个对话（或每次协作运行）各记各的草稿：切到别的窗口再切回来，没发出去的文字与附件还在原处。
 *
 * 存在 localStorage（与「已关闭的提醒」同一套路）：隐私模式、配额用尽一律降级成「没有记忆」，
 * 不影响输入本身。只记**待发送的内容**；内容清空（发送成功 / 用户删光）时就把这条记忆抹掉。
 *
 * 记忆挂在「这段草稿属于谁」上，而不是「当前路由是谁」——切换的那一瞬间两者并不一致，
 * 用路由 id 当 key 会把上一段文字顺手存到新对话名下（这正是要修的毛病）。
 */
import { useCallback, useEffect, useRef, useState, type SetStateAction } from 'react'
import type { AttachmentRef } from './types'

/** 一段待发送内容：正文 + 待发附件。 */
export interface ComposerDraft {
  text: string
  attachments: AttachmentRef[]
}

const STORAGE_KEY = 'arkswarm.composer.drafts'
/** 最多记住多少段草稿：超出的按写入先后淘汰，避免本地存储无限膨胀。 */
const MAX_DRAFTS = 50

function emptyDraft(): ComposerDraft {
  return { text: '', attachments: [] }
}

type Store = Record<string, ComposerDraft>

/** 附件是从本地存储读回来的数据：逐字段验一遍，坏数据直接丢掉，别把界面搞崩。 */
function isAttachment(value: unknown): value is AttachmentRef {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<AttachmentRef>
  return (
    typeof item.name === 'string' &&
    typeof item.rel_path === 'string' &&
    typeof item.path === 'string' &&
    typeof item.size === 'number'
  )
}

function readStore(): Store {
  if (typeof localStorage === 'undefined') return {}
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    const store: Store = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!value || typeof value !== 'object') continue
      const item = value as Partial<ComposerDraft>
      const text = typeof item.text === 'string' ? item.text : ''
      const attachments = Array.isArray(item.attachments) ? item.attachments.filter(isAttachment) : []
      if (!text && attachments.length === 0) continue
      store[key] = { text, attachments }
    }
    return store
  } catch {
    // 本地存储不可用（隐私模式）/ 数据损坏：当作没有记忆，界面照常工作
    return {}
  }
}

function writeStore(store: Store): void {
  if (typeof localStorage === 'undefined') return
  try {
    const keys = Object.keys(store)
    for (const stale of keys.slice(0, Math.max(0, keys.length - MAX_DRAFTS))) delete store[stale]
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store))
  } catch {
    // 写不进去就算了：最坏情况是切回来草稿没了，不影响发送
  }
}

/** 读某个窗口的草稿；没有记忆时给一段空的。 */
export function loadComposerDraft(id: string | null | undefined): ComposerDraft {
  if (!id) return emptyDraft()
  return readStore()[id] ?? emptyDraft()
}

/**
 * 还留着草稿的窗口 id 集合（正文非空或有待发附件）。
 *
 * 「新建对话」按钮用它判断某条空对话算不算「被用过」——没发消息但输入框里有字，
 * 说明用户正在里面打草稿，再点新建就该真开一条新的（见 lib/conversations）。
 * 一次读盘批量判断，别在循环里逐条 `loadComposerDraft`（那样每条都要整库 parse 一遍）。
 */
export function draftOwnerIds(): Set<string> {
  return new Set(Object.keys(readStore()))
}

/** 记下某个窗口的草稿；内容全空等于「没有草稿」，顺手抹掉这条记忆。 */
export function saveComposerDraft(id: string, draft: ComposerDraft): void {
  const store = readStore()
  if (!draft.text && draft.attachments.length === 0) delete store[id]
  else store[id] = { text: draft.text, attachments: draft.attachments }
  writeStore(store)
}

/**
 * 把「当前窗口的草稿」接进组件。返回的四个值用法与原地的 `useState` 完全一致（含函数式更新）：
 *
 * ```tsx
 * const { text: draft, setText: setDraft, attachments, setAttachments } = useComposerDraft(conversationId)
 * ```
 *
 * 切窗口时草稿按 key 各自取回，且是**渲染期同步**换掉（不是 useEffect）——否则会先闪一帧
 * 上一段对话的文字，那正是用户看到的「文字跳到别的窗口」。
 */
export function useComposerDraft(id: string | null | undefined) {
  const key = id ?? null
  const [state, setState] = useState<{ owner: string | null; value: ComposerDraft }>(() => ({
    owner: key,
    value: loadComposerDraft(key),
  }))

  // 换了窗口：这一帧就把新窗口的草稿摆好。React 会立刻用新状态重渲染，中间态不会上屏。
  if (state.owner !== key) setState({ owner: key, value: loadComposerDraft(key) })

  // 值一变就落盘。用 state.owner（这段草稿属于谁）而不是路由参数当 key，避免存错窗口。
  // `latest` 同步在这里更新（不在渲染期写 ref）：被丢弃的并发渲染不会把旧文字覆盖回来。
  const latest = useRef(state)
  useEffect(() => {
    latest.current = state
    if (state.owner) saveComposerDraft(state.owner, state.value)
  }, [state])

  // 离开页面时再补一次：最后那一帧的改动还没等到上面的 effect 也不会丢。
  useEffect(
    () => () => {
      const { owner, value } = latest.current
      if (owner) saveComposerDraft(owner, value)
    },
    [],
  )

  const setText = useCallback((next: SetStateAction<string>) => {
    setState((prev) => ({
      ...prev,
      value: { ...prev.value, text: typeof next === 'function' ? next(prev.value.text) : next },
    }))
  }, [])

  const setAttachments = useCallback((next: SetStateAction<AttachmentRef[]>) => {
    setState((prev) => ({
      ...prev,
      value: {
        ...prev.value,
        attachments: typeof next === 'function' ? next(prev.value.attachments) : next,
      },
    }))
  }, [])

  return { text: state.value.text, setText, attachments: state.value.attachments, setAttachments }
}
