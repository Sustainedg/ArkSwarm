import { useEffect, type ReactNode } from 'react'

interface Props {
  title: string
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  /** 需要接近全屏时使用（例如长表单、架构文档） */
  full?: boolean
  /** 需要让内部某块自己滚动时，给 body 加专用类名 */
  bodyClassName?: string
}

/** 移动端底部弹层：替代桌面端的居中 Modal，更贴近手机手势习惯。 */
export function MobileSheet({ title, onClose, children, footer, full, bodyClassName }: Props) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="m-sheet-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className={`m-sheet${full ? ' full' : ''}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="m-sheet-grab" />
        <div className="m-sheet-head">
          <h2>{title}</h2>
          <button className="m-icon-btn" onClick={onClose} aria-label="关闭">
            ✕
          </button>
        </div>
        <div className={`m-sheet-body${bodyClassName ? ` ${bodyClassName}` : ''}`}>{children}</div>
        {footer && <div className="m-sheet-foot">{footer}</div>}
      </div>
    </div>
  )
}
