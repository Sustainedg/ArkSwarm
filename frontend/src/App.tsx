import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { ChatPage } from './pages/ChatPage'
import { AgentsPage } from './pages/AgentsPage'
import { RolesPage } from './pages/RolesPage'
import { SettingsPage } from './pages/SettingsPage'

const NAV = [
  { to: '/chat', icon: '💬', label: '对话' },
  { to: '/roles', icon: '🎭', label: '角色' },
  { to: '/agents', icon: '⚙️', label: 'Agent 配置' },
  { to: '/settings', icon: '📊', label: '系统' },
]

export default function App() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">✦</div>
          <div className="brand-text">
            <div className="brand-name">Magenta</div>
            <div className="brand-sub">Agent 控制台</div>
          </div>
        </div>
        <nav className="stack" style={{ gap: 4 }}>
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-label">{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer">
          配置 Agent → 创建角色 → 开始对话
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ChatPage />} />
          <Route path="/roles" element={<RolesPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </main>
    </div>
  )
}
