/**
 * 协作岗位的实时对话线程（共享组件）。
 * 桌面端岗位面板与移动端岗位弹层都用它：内部把「岗位出入的步骤 / 工单 / 流式输出」
 * 装配成统一的 `ConversationThread` 条目，气泡外观与单聊、协作主对话完全一致。
 */
import { useEffect, useMemo, useRef, type ReactNode } from 'react'
import { ACTIONS } from '../lib/collabDraft'
import type { FinishedStep, LiveStep } from '../lib/collabStream'
import { stepHasOutput, stepStatusHint, type CollabAgentProgress } from '../lib/collabProgress'
import { buildAgentThread, ticketHighlights } from '../lib/collabThread'
import type { CollabStep, CollabTicket } from '../lib/types'
import { ConversationThread, type ConversationThreadItem } from './ConversationThread'
import type { RenderMessage } from './MessageView'

interface Props {
  node: CollabAgentProgress
  roster: CollabAgentProgress[]
  steps: CollabStep[]
  tickets: CollabTicket[]
  live: LiveStep | null
  streamed: FinishedStep[]
  /** 容器类名：桌面端用 `collab-agent-thread`，移动端用 `m-collab-agent-thread` */
  className?: string
  empty?: ReactNode
}

const ACTION_LABELS = Object.fromEntries(ACTIONS.map((item) => [item.value, item.label]))

const STEP_STATUS_LABELS: Record<string, string> = {
  streaming: '进行中',
  interrupted: '已中断',
  error: '出错',
  cancelled: '已取消',
}

export function CollabAgentThread({
  node, roster, steps, tickets, live, streamed, className, empty,
}: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  // 正在流式输出的那一步可能同时出现在落库详情里（跨设备刷新时），按 stepId 去重避免重复。
  const ownSteps = useMemo(
    () => steps.filter((step) => step.role_key === node.key && step.id !== live?.stepId),
    [steps, node.key, live?.stepId],
  )
  const ownStreamed = useMemo(() => {
    const persisted = new Set(ownSteps.map((step) => step.id))
    return streamed.filter((step) => step.nodeKey === node.key && !persisted.has(step.stepId))
  }, [ownSteps, node.key, streamed])
  const liveHere = live?.nodeKey === node.key ? live : null
  const ticketTitle = useMemo(
    () => Object.fromEntries(tickets.map((ticket) => [ticket.id, ticket.title])),
    [tickets],
  )
  const labelOf = (key: string) => roster.find((item) => item.key === key)?.label ?? key
  const avatarOf = (key: string) => roster.find((item) => item.key === key)?.avatar ?? '🤖'
  const thread = useMemo(
    () => buildAgentThread({ nodeKey: node.key, isEntry: node.isEntry, steps, tickets, labelOf, avatarOf }),
    // labelOf/avatarOf 只是 roster 的查询，依赖 roster 即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [node.key, node.isEntry, steps, tickets, roster],
  )

  const liveMessage: RenderMessage | null = liveHere
    ? {
        id: `live-${liveHere.stepId}`,
        role: 'assistant',
        content: liveHere.agent.text,
        parts: liveHere.agent.parts,
        status: liveHere.agent.error ? 'error' : 'streaming',
        error: liveHere.agent.error,
        usage: liveHere.agent.usage,
        streaming: true,
        logs: liveHere.agent.logs,
      }
    : null

  const threadItems: ConversationThreadItem[] = []
  for (const entry of thread) {
    if (entry.kind === 'user') {
      threadItems.push({
        id: entry.id,
        message: { id: entry.id, role: 'user', content: entry.text ?? '', parts: [], status: 'complete' },
      })
      continue
    }

    if (entry.kind === 'handoff') {
      const highlights = ticketHighlights(entry.ticket)
      threadItems.push({
        id: entry.id,
        custom: (
          <div className="bubble-row user collab-agent-handoff-row">
            <div className="avatar user">{entry.fromAvatar}</div>
            <div className="bubble">
              <div className="bubble-head">
                <span>{entry.fromLabel}</span>
                <span>→ {node.label}</span>
                <span className="pill brand">交接</span>
              </div>
              <div className="collab-agent-handoff">
                {entry.text && <div className="collab-agent-handoff-text">{entry.text}</div>}
                {entry.ticket && (
                  <div className="collab-agent-handoff-ticket">
                    <div className="collab-agent-handoff-title">📋 {entry.ticket.title}</div>
                    {highlights.map((line) => (
                      <div className="dim small" key={line}>{line}</div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        ),
      })
      continue
    }

    const step = entry.step!
    const hint = stepStatusHint(step.status, stepHasOutput(step))
    threadItems.push({
      id: entry.id,
      className: step.status === 'streaming' ? 'live' : undefined,
      meta: (
        <>
          <span>#{step.sequence}</span>
          {step.action && <span className="pill">{ACTION_LABELS[step.action] ?? step.action}</span>}
          {step.ticket_id && ticketTitle[step.ticket_id] && <span>工单：{ticketTitle[step.ticket_id]}</span>}
          {step.status !== 'complete' && (
            <span className={`pill ${step.status === 'error' || step.status === 'interrupted' ? 'err' : ''}`}>
              {STEP_STATUS_LABELS[step.status] ?? step.status}
            </span>
          )}
        </>
      ),
      custom: hint ? (
        <div className={`collab-agent-placeholder${step.status === 'streaming' ? ' live' : ''}`}>{hint}</div>
      ) : undefined,
      message: hint ? undefined : {
        id: step.id,
        role: 'assistant',
        content: step.content,
        parts: step.parts,
        status: step.status,
        error: step.error,
        usage: step.usage,
      },
      avatar: node.avatar,
      label: node.label,
    })
  }

  for (const step of ownStreamed) {
    threadItems.push({
      id: step.stepId,
      className: step.status === 'streaming' ? 'live' : undefined,
      meta: (
        <>
          {step.action && <span className="pill">{ACTION_LABELS[step.action] ?? step.action}</span>}
          {step.ticketId && ticketTitle[step.ticketId] && <span>工单：{ticketTitle[step.ticketId]}</span>}
          {step.status !== 'complete' && <span className="pill">{step.status}</span>}
        </>
      ),
      message: {
        id: step.stepId,
        role: 'assistant',
        content: step.agent.text,
        parts: step.agent.parts,
        status: step.error ? 'error' : step.status,
        error: step.error ?? step.agent.error,
        usage: step.agent.usage,
        logs: step.agent.logs,
      },
      avatar: node.avatar,
      label: node.label,
    })
  }

  if (liveMessage) {
    threadItems.push({
      id: liveMessage.id,
      className: 'live',
      meta: <span className="pill brand">实时输出</span>,
      message: liveMessage,
      avatar: node.avatar,
      label: node.label,
    })
  }

  const liveText = liveHere?.agent.text ?? ''
  const liveParts = liveHere?.agent.parts.length ?? 0
  // 内容变化（新步骤、流式追加、切换岗位）时把线程滚到底部。
  useEffect(() => {
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
  }, [node.key, threadItems.length, liveText, liveParts])

  return (
    <ConversationThread
      className={className}
      threadRef={scrollRef}
      items={threadItems}
      empty={thread.length === 0 && ownStreamed.length === 0 && !liveMessage ? empty : undefined}
    />
  )
}
