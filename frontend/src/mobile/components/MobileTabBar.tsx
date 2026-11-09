import { NavLink } from 'react-router-dom'

/** 移动端主导航：固定在底部，5 个入口（智能体配置从「设置」进入）。 */
const TABS = [
  { to: '/chat', icon: '💬', label: '对话' },
  { to: '/roles', icon: '🎭', label: '角色' },
  { to: '/collab', icon: '🤝', label: '协作' },
  { to: '/skills', icon: '🧩', label: '技能' },
  { to: '/settings', icon: '⚙️', label: '设置' },
]

export function MobileTabBar() {
  return (
    <nav className="m-tabbar" aria-label="主导航">
      {TABS.map((tab) => (
        <NavLink key={tab.to} to={tab.to} className={({ isActive }) => `m-tab${isActive ? ' active' : ''}`}>
          <span className="m-tab-icon">{tab.icon}</span>
          <span>{tab.label}</span>
        </NavLink>
      ))}
    </nav>
  )
}
