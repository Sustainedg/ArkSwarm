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

        {timeline.map((part, index) => (
          <TimelinePart
            key={`${part.kind}-${index}`}
            part={part}
            active={Boolean(message.streaming)}
            cursor={Boolean(message.streaming) && index === lastIndex && part.kind === 'text'}
          />
        ))}

        {showTrailingCursor && <div className="bubble-body streaming" />}

        {message.logs && message.logs.length > 0 && (
          <details className="tool-card">
            <summary>
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

function TimelinePart({ part, active, cursor }: { part: MessagePart; active: boolean; cursor: boolean }) {
  if (part.kind === 'thinking') {
    return (
      <details className="tool-card thinking-card" open={active}>
        <summary>
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
