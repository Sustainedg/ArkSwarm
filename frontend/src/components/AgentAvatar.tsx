/**
 * 智能体头像（两端共用）：自定义上传图 > 内置头像包 > emoji，三者依次回退。
 *
 * 与 `UserAvatar` 同一套外形约定：尺寸走内联 `width/height`（调用方原样式就是内联的），
 * 图片撑满圆框；加载失败（文件丢了 / 取图失败）自动退回 emoji，界面里不出现裂图。
 *
 * 「没图显示 emoji」不只是兜底：协作办公室地图、IM 绑定等只认得到 emoji 字段的界面，
 * 依旧显示 `avatar`，所以选了头像包也不会在那边变成一串编号。
 */
import { useEffect, useState } from 'react'
import { avatarPresetUrl, isAvatarPreset, useAgentAvatarFileUrl } from '../lib/agentAvatar'

export function AgentAvatar({
  avatar,
  avatarPreset,
  avatarFile,
  agentId,
  size,
  className,
  title,
}: {
  /** emoji 头像（最老的字段，永远有值可回退） */
  avatar?: string | null
  /** 内置头像包编号；有值时渲染 /avatars/<它>.webp */
  avatarPreset?: string | null
  /** 自定义头像文件名；有值时取 /api/agents/{id}/avatar */
  avatarFile?: string | null
  /** 智能体 id：取自定义图要用；没有就跳过自定义图这一层 */
  agentId?: string | null
  /** 边长（px）；不传就撑满父级（CSS 里按 100% 处理） */
  size?: number
  className?: string
  title?: string
}) {
  const fileUrl = useAgentAvatarFileUrl(agentId, avatarFile)
  const [broken, setBroken] = useState(false)
  // 换了图重置裂图标记：上一次加载失败不代表这一次也失败
  useEffect(() => setBroken(false), [fileUrl, avatarFile, avatarPreset])
  const preset = !avatarFile && isAvatarPreset(avatarPreset) ? avatarPreset : null
  const showFile = Boolean(agentId && avatarFile && fileUrl && !broken)
  const showPreset = !showFile && Boolean(preset) && !broken
  const style = size
    ? ({ width: size, height: size, fontSize: Math.round(size * 0.47) } as React.CSSProperties)
    : undefined

  return (
    <span className={['agent-avatar', className].filter(Boolean).join(' ')} style={style} title={title}>
      {showFile || showPreset ? (
        <img
          src={showFile ? (fileUrl ?? undefined) : avatarPresetUrl(preset ?? '')}
          alt=""
          onError={() => setBroken(true)}
        />
      ) : (
        avatar || '🤖'
      )}
    </span>
  )
}
