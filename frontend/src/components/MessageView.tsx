import type { MessagePart } from '../lib/types'
import { summarizeUsage } from '../lib/usage'
import { MarkdownView } from './MarkdownView'

export interface RenderMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  parts: MessagePart[]
  status: string
  error?: string | null
  usage?: Record<string, number>
  streaming?: boolean
  logs?: string[]
}

export function MessageView({ message, avatar, label }: { message: RenderMessage; avatar?: string; label?: string }) {
  const isUser = message.role === 'user'
  const outputTokens = summarizeUsage([message.usage]).output

  // 一律按真实时间线渲染：思考 / 正文 / 工具调用与结果保持它们实际发生的先后顺序。
  // 旧消息只有 content 与工具、思考片段（没有内联正文段），这里把 content 合成一段兜底，
  // 并保持「思考在前、正文其次、工具在后」。
  const hasInlineText = message.parts.some((p) => p.kind === 'text')
  const timeline: MessagePart[] = hasInlineText
    ? message.parts
    : [
        ...message.parts.filter((p) => p.kind === 'thinking'),
        ...(message.content ? [{ kind: 'text', text: message.content } as MessagePart] : []),
        ...message.parts.filter((p) => p.kind !== 'thinking' && p.kind !== 'text'),
      ]

  const lastIndex = timeline.length - 1
  const showTrailingCursor =
    Boolean(message.streaming) && (timeline.length === 0 || timeline[lastIndex].kind !== 'text')

  // 连续的「思考 / 工具调用 / 工具结果」收成一个可展开的「过程」块：
  // 默认折叠成一行，点开才按原顺序逐条查看；正文原样内联，整条时间线顺序不变。
  const blocks = groupTimeline(timeline)

  return (
    <div className={`bubble-row${isUser ? ' user' : ''}`}>
      <div className={`avatar${isUser ? ' user' : ''}`}>{isUser ? '🙋' : (avatar ?? '🤖')}</div>
      <div className="bubble">
        <div className="bubble-head">
          <span>{isUser ? '你' : (label ?? '智能体')}</span>
          {outputTokens > 0 && <span className="dim">{`${outputTokens.toLocaleString()} tok`}</span>}
          {message.status === 'error' && <span className="pill err">出错</span>}
          {message.status === 'interrupted' && <span className="pill warn">已中断</span>}
        </div>

        {blocks.map((block) =>
          block.kind === 'text' ? (
            <div
              key={`text-${block.index}`}
              className={`bubble-body${
                Boolean(message.streaming) && block.index === lastIndex ? ' streaming' : ''
              }`}
            >
              <MarkdownView text={block.part.text ?? ''} />
            </div>
          ) : (
            <ProcessFold key={`process-${block.items[0].index}`} items={block.items} />
          ),
        )}

        {showTrailingCursor && <div className="bubble-body streaming" />}

        {message.logs && message.logs.length > 0 && (
          <details className="tool-card">
            <summary>
              <DisclosureCaret />
              <span className="tool-chip">日志</span>
              <span className="dim">{message.logs.length} 条运行日志</span>
            </summary>
            <pre>{message.logs.join('\n')}</pre>
          </details>
        )}

        {message.error && <div className="banner err" style={{ marginTop: 8 }}>{message.error}</div>}
      </div>
    </div>
  )
}

type TimelineItem = { part: MessagePart; index: number }
type TimelineBlock =
  | { kind: 'text'; part: MessagePart; index: number }
  | { kind: 'process'; items: TimelineItem[] }

/** 把时间线切成「正文」与「连续的过程片段」两种块，只做归并、不改变先后顺序。 */
function groupTimeline(timeline: MessagePart[]): TimelineBlock[] {
  const blocks: TimelineBlock[] = []
  timeline.forEach((part, index) => {
    if (part.kind === 'text') {
      blocks.push({ kind: 'text', part, index })
      return
    }
    const last = blocks[blocks.length - 1]
    if (last && last.kind === 'process') last.items.push({ part, index })
    else blocks.push({ kind: 'process', items: [{ part, index }] })
  })
  return blocks
}

/**
 * 一段连续的「思考 / 工具调用 / 工具结果」：默认收成一行，点标题栏展开后按原顺序列出每一步，
 * 每一步还能再单独点开看详情。这样一轮几十次工具调用也只是一行，不再「每次调用都占一条」。
 */
function ProcessFold({ items }: { items: TimelineItem[] }) {
  const thinking = items.filter((item) => item.part.kind === 'thinking').length
  const tools = items.length - thinking
  const summary = [thinking > 0 ? `思考 ${thinking}` : '', tools > 0 ? `工具调用 ${tools}` : '']
    .filter(Boolean)
    .join(' · ')
  return (
    <details className="tool-card process-fold">
      <summary>
        <DisclosureCaret />
        <span className="tool-chip">过程</span>
        <span className="dim">{summary}</span>
        <span className="dim small process-fold-hint">点击展开</span>
      </summary>
      <div className="process-fold-body">
        {items.map(({ part, index }) => (
          <TimelinePart key={`${part.kind}-${index}`} part={part} cursor={false} />
        ))}
      </div>
    </details>
  )
}

function TimelinePart({ part, cursor }: { part: MessagePart; cursor: boolean }) {
  if (part.kind === 'thinking') {
    return (
      <details className="tool-card thinking-card">
        <summary>
          <DisclosureCaret />
          <span className="tool-chip">思考</span>
          <span className="dim">推理过程</span>
        </summary>
        <pre className="thinking" style={{ borderTop: 'none' }}>{part.text}</pre>
      </details>
    )
  }
  if (part.kind === 'text') {
    return (
      <div className={`bubble-body${cursor ? ' streaming' : ''}`}>
        <MarkdownView text={part.text ?? ''} />
      </div>
    )
  }
  return <ToolCard part={part} />
}

function ToolCard({ part }: { part: MessagePart }) {
  const isCall = part.kind === 'tool_call'
  const label = isCall ? '调用' : part.is_error ? '错误' : '结果'
  const payload = isCall ? JSON.stringify(part.input ?? {}, null, 2) : (part.output ?? '')
  return (
    <details className="tool-card">
      <summary>
        <DisclosureCaret />
        <span className={`tool-chip${part.is_error ? ' err' : ''}`}>{part.name ?? label}</span>
        <span className="dim">{isCall ? '工具调用' : '工具结果'}</span>
        {!isCall && <span className="dim mono-sm" style={{ marginLeft: 'auto' }}>
          {String(payload).split('\n').length} 行
        </span>}
      </summary>
      <pre>{payload || '(空)'}</pre>
    </details>
  )
}

/** 折叠箭头：收起时指右，展开时转 90°。纯装饰，点击由原生 details 处理。 */
function DisclosureCaret() {
  return <span className="disclosure-caret" aria-hidden>▶</span>
}
