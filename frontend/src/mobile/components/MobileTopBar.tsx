import type { ReactNode } from 'react'
import { MobileMenuButton } from './MobileMenuButton'

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
  /** 标题**居中**显示（对话页的对话名称用）：左右两侧让出按钮的位置，长标题自动省略。 */
  centered?: boolean
}

/**
 * 移动端顶栏：左侧返回/菜单 + 标题/副标题 + 右侧操作，自动避开状态栏。
 *
 * 顶级页面（没有 `onBack`）默认给一个打开左侧抽屉的 logo 按钮；
 * 传入 `leading` 时以 `leading` 为准（子页面通常是返回箭头）。
 * `centered` 时标题脱离文档流居中 —— 对话页只用它显示对话名称，不再显示智能体名与内核说明。
 */
export function MobileTopBar({ title, subtitle, onBack, actions, badge, leading, centered }: Props) {
  const back = onBack && (
    <button className="m-icon-btn" onClick={onBack} aria-label="返回">
      ‹
    </button>
  )
  const left = leading ?? (!onBack && <MobileMenuButton />)
  if (centered) {
    return (
      <header className="m-topbar centered">
        {back}
        {left}
        <div className="spacer" />
        <div className="m-topbar-centered" title={typeof title === 'string' ? title : undefined}>
          <span>{title}</span>
          {badge}
        </div>
        {actions}
      </header>
    )
  }
  return (
    <header className="m-topbar">
      {back}
      {left}
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
