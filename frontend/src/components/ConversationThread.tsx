/** 统一对话线程：单聊、协作对话、岗位实时对话共用同一套气泡布局与间距。 */
import type { ReactNode, Ref } from 'react'
import { MessageView, type RenderMessage } from './MessageView'

export interface ConversationThreadItem {
  id: string
  message?: RenderMessage
  avatar?: string
  label?: string
  /** 气泡上方的岗位 / 工单元信息 */
  meta?: ReactNode
  /** 非普通消息内容（例如工单交接卡） */
  custom?: ReactNode
  className?: string
}

interface Props {
  items: ConversationThreadItem[]
  empty?: ReactNode
  /** 单聊与协作主对话居中限宽；岗位进度面板保持铺满 */
  centered?: boolean
  className?: string
  threadRef?: Ref<HTMLDivElement>
}

export function ConversationThread({ items, empty, centered = false, className = '', threadRef }: Props) {
  const classes = ['conversation-thread', centered ? 'centered' : '', className].filter(Boolean).join(' ')
  return (
    <div className={classes} ref={threadRef}>
      {items.length === 0 && empty}
      {items.map((item) => (
        <div key={item.id} className={['conversation-turn', item.className ?? ''].filter(Boolean).join(' ')}>
          {item.meta && <div className="conversation-turn-meta">{item.meta}</div>}
          {item.custom ?? (item.message && (
            <MessageView message={item.message} avatar={item.avatar} label={item.label} />
          ))}
        </div>
      ))}
    </div>
  )
}
