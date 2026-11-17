/**
 * 用户头像（两端共用）：有图就显示图，没图就用「显示名首字母」的渐变圆。
 *
 * 尺寸走 CSS 变量 `--avatar-size` 而不是内联的 `width`：侧边栏那套布局用 `--sbs` 随
 * 侧栏宽度缩放，内联尺寸会把它顶掉。默认值写在 `.user-avatar` 里，需要局部改大小的
 * 地方（侧栏折叠、抽屉）在自己的选择器上覆盖 `--avatar-size` 即可。
 *
 * 头像加载失败（文件丢了 / 请求失败）也自动退回首字母 —— 界面里不出现裂图。
 */
import { useState } from 'react'
import { avatarInitial, useAvatarUrl } from '../lib/avatar'
import type { AuthUser } from '../lib/types'

export function UserAvatar({
  user,
  size,
  className,
  title,
}: {
  user: Pick<AuthUser, 'id' | 'avatar' | 'display_name' | 'username'> | null | undefined
  /** 边长（px）；不传就用样式表里的默认值 */
  size?: number
  className?: string
  title?: string
}) {
  const url = useAvatarUrl(user)
  const [broken, setBroken] = useState(false)
  const name = user?.display_name || user?.username || ''
  const showImage = Boolean(url) && !broken

  return (
    <span
      className={`user-avatar${className ? ` ${className}` : ''}`}
      style={size ? ({ ['--avatar-size' as string]: `${size}px` } as React.CSSProperties) : undefined}
      title={title}
      aria-hidden="true"
    >
      {showImage ? (
        <img src={url ?? undefined} alt="" onError={() => setBroken(true)} />
      ) : (
        avatarInitial(name)
      )}
    </span>
  )
}
