import type { ReactNode } from 'react'

interface Props {
  title: ReactNode
  subtitle?: ReactNode
  /** 传入即显示返回箭头 */
  onBack?: () => void
  /** 右侧操作区（放图标按钮） */
  actions?: ReactNode
  /** 标题后缀（版本号等） */
  badge?: ReactNode
  /** 标题左侧的图标/头像 */
  leading?: ReactNode
}

/** 移动端顶栏：左侧返回 + 标题/副标题 + 右侧操作，自动避开状态栏。 */
export function MobileTopBar({ title, subtitle, onBack, actions, badge, leading }: Props) {
  return (
    <header className="m-topbar">
      {onBack && (
        <button className="m-icon-btn" onClick={onBack} aria-label="返回">
          ‹
        </button>
      )}
      {leading}
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="m-topbar-title">
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{title}</span>
          {badge}
        </div>
        {subtitle && <div className="m-topbar-sub">{subtitle}</div>}
      </div>
      {actions}
    </header>
  )
}
