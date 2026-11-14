import type { ReactNode } from 'react'

interface Props {
  /** 分组标题（内核名 / 智能体分组名）。 */
  title: ReactNode
  /** 右侧的数量徽章（可选）。 */
  count?: number
  /** 是否展开（状态由页面持有：默认折叠就是初始 false）。 */
  expanded: boolean
  onToggle: () => void
  children: ReactNode
}

/**
 * 可展开 / 折叠的分组：标题一行（点一下切换），内容只在展开时渲染。
 *
 * 两端共用的纯展示组件（不含数据）：技能页按内核分组、智能体页按分组展示都用它。
 * 样式两端各写一份、共享段逐字一致（见 index.css / mobile.css 的「折叠分组」横幅）。
 */
export function CollapsibleGroup({ title, count, expanded, onToggle, children }: Props) {
  return (
    <section className="collapse-group">
      <button
        type="button"
        className={`collapse-head${expanded ? ' open' : ''}`}
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <span className="collapse-caret" aria-hidden>
          ▶
        </span>
        <span className="collapse-title">{title}</span>
        {count !== undefined && <span className="collapse-count">{count}</span>}
      </button>
      {expanded && <div className="collapse-body">{children}</div>}
    </section>
  )
}
