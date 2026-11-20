/**
 * 对话列表行首的头像（桌面侧栏与移动抽屉共用）。
 *
 * - **单聊**：这条对话只归一个智能体，画一枚圆头像；
 * - **群聊**：用过 @ 召唤的对话里出现过多个智能体，照群聊列表的样子把头像叠成一摞
 *   （最多 3 枚，多出来的用「+N」补齐）。主场智能体永远排第一，召唤者按首次出场先后。
 *
 * 头像来源沿用 `AgentAvatar` 的三层回退（自定义上传 > 内置头像包 > emoji）；
 * 后端没给 `participants`（老版本接口）时就拿对话自身的智能体快照兜底，不会出现空白。
 */
import type { Conversation, ConversationParticipant } from '../lib/types'
import { AgentAvatar } from './AgentAvatar'

/** 堆叠时最多画几枚头像；再多就用「+N」收口，免得把窄侧栏的标题挤没。 */
const MAX_STACK = 3

function fallbackParticipant(conversation: Conversation): ConversationParticipant {
  return {
    id: conversation.agent_profile_id,
    name: conversation.agent_name,
    avatar: conversation.agent_avatar,
    avatar_preset: conversation.agent_avatar_preset,
    avatar_file: conversation.agent_avatar_file,
  }
}

export function ConversationAvatar({ conversation }: { conversation: Conversation }) {
  const participants =
    conversation.participants && conversation.participants.length > 0
      ? conversation.participants
      : [fallbackParticipant(conversation)]
  const stacked = participants.length > 1
  const shown = stacked ? participants.slice(0, MAX_STACK) : participants
  const extra = participants.length - shown.length
  const names = participants.map((participant) => participant.name || '智能体').join('、')

  return (
    <span
      className={`conv-avatars${stacked ? ' stacked' : ''}`}
      // 头像只是视觉标识：读屏走 title / 正文里的智能体名，不重复念一串名字。
      aria-hidden="true"
      title={stacked ? `参与智能体：${names}` : names}
    >
      {shown.map((participant) => (
        <AgentAvatar
          key={participant.id}
          avatar={participant.avatar}
          avatarPreset={participant.avatar_preset}
          avatarFile={participant.avatar_file}
          agentId={participant.id}
          className="conv-avatar"
        />
      ))}
      {extra > 0 && <span className="conv-avatar conv-avatar-more">+{extra}</span>}
    </span>
  )
}
