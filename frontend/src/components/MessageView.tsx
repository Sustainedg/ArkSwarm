import type { MessagePart } from '../lib/types'

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

export function MessageView({ message, avatar }: { message: RenderMessage; avatar?: string }) {
  const isUser = message.role === 'user'
  const thinking = message.parts.filter((p) => p.kind === 'thinking')
  const tools = message.parts.filter((p) => p.kind !== 'thinking')

  return (
    <div className={`bubble-row${isUser ? ' user' : ''}`}>
      <div className={`avatar${isUser ? ' user' : ''}`}>{isUser ? '🙋' : (avatar ?? '🤖')}</div>
      <div className="bubble" style={{ flex: 1 }}>
        <div className="bubble-head">
          <span>{isUser ? '你' : '智能体'}</span>
          {message.usage && Object.keys(message.usage).length > 0 && (
            <span className="dim">
              {message.usage.output_tokens ? `${message.usage.output_tokens} tok` : ''}
            </span>
          )}
          {message.status === 'error' && <span className="pill err">出错</span>}
          {message.status === 'interrupted' && <span className="pill warn">已中断</span>}
        </div>

        {message.content ? (
          <div className={`bubble-body${message.streaming ? ' streaming' : ''}`}>{message.content}</div>
        ) : (
          message.streaming && <div className="bubble-body streaming" />
        )}

        {thinking.map((part, index) => (
          <details className="tool-card" key={`think-${index}`} open={Boolean(message.streaming)}>
            <summary>
              <span className="tool-chip">思考</span>
              <span className="dim">推理过程</span>
            </summary>
            <pre className="thinking" style={{ borderTop: 'none' }}>{part.text}</pre>
          </details>
        ))}

        {tools.map((part, index) => (
          <ToolCard key={`tool-${index}`} part={part} />
        ))}

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
