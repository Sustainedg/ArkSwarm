/** 统一对话线程：单聊、协作对话、岗位实时对话共用同一套气泡布局与间距。 */
import type { ReactNode, Ref } from 'react'
import type { AuthUser } from '../lib/types'
import { MessageView, type RenderMessage } from './MessageView'

export interface ConversationThreadItem {
  id: string
  message?: RenderMessage
  avatar?: string
  /** 智能体的内置头像包编号 / 自定义头像文件：气泡画真头像用（见 AgentAvatar） */
  avatarPreset?: string | null
  avatarFile?: string | null
  /** 智能体 id：取自定义头像图要用 */
  agentId?: string | null
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
  /** 「只看最终回复」：传给每条消息（不传保持完整视图，协作页等不传即不受影响） */
  finalOnly?: boolean
  /** 当前登录用户：自己发的消息画真头像（不传退回 🙋） */
  user?: Pick<AuthUser, 'id' | 'avatar' | 'display_name' | 'username'> | null
}

export function ConversationThread({ items, empty, centered = false, className = '', threadRef, finalOnly = false, user }: Props) {
  const classes = ['conversation-thread', centered ? 'centered' : '', className].filter(Boolean).join(' ')
  return (
    <div className={classes} ref={threadRef}>
      {items.length === 0 && empty}
      {items.map((item) => (
        <div key={item.id} className={['conversation-turn', item.className ?? ''].filter(Boolean).join(' ')}>
          {item.meta && <div className="conversation-turn-meta">{item.meta}</div>}
          {item.custom ?? (item.message && (
            <MessageView
              message={item.message}
              avatar={item.avatar}
              avatarPreset={item.avatarPreset}
              avatarFile={item.avatarFile}
              agentId={item.agentId}
              label={item.label}
              finalOnly={finalOnly}
              user={user}
            />
          ))}
        </div>
      ))}
    </div>
  )
}
