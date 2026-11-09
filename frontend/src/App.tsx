import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './lib/api'
import { ChatPage } from './pages/ChatPage'
import { AgentsPage } from './pages/AgentsPage'
import { RolesPage } from './pages/RolesPage'
import { SettingsPage } from './pages/SettingsPage'

const NAV = [
  { to: '/chat', icon: '💬', label: '对话' },
  { to: '/roles', icon: '🎭', label: '角色' },
  { to: '/agents', icon: '⚙️', label: '智能体配置' },
  { to: '/settings', icon: '📊', label: '系统' },
]

export default function App() {
  const [version, setVersion] = useState('')

  useEffect(() => {
    api
      .overview()
      .then((info) => setVersion(info.version))
      .catch(() => undefined)
  }, [])

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">✦</div>
          <div className="brand-text">
            <div className="brand-name">
              Magenta
              {version && <span className="brand-version">{version}</span>}
            </div>
            <div className="brand-sub">多智能体控制台</div>
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
          配置智能体 → 创建角色 → 开始对话
          {version && <div style={{ marginTop: 4 }}>版本 {version}</div>}
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
