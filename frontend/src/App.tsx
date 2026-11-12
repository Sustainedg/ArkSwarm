import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { ConversationList } from './components/ConversationList'
import { api } from './lib/api'
import { ChatPage } from './pages/ChatPage'
import { CollabPage } from './pages/CollabPage'
import { AgentsPage } from './pages/AgentsPage'
import { RolesPage } from './pages/RolesPage'
import { RuntimesPage } from './pages/RuntimesPage'
import { SkillsPage } from './pages/SkillsPage'
import { SettingsPage } from './pages/SettingsPage'

//: 与移动端抽屉的主导航**同一份菜单**（顺序、命名、图标都一样）：
//: 「智能体配置」「运行时」不在菜单里，从「设置」页的配置入口进（移动端就是这么放的）。
const NAV = [
  { to: '/chat', icon: '💬', label: '对话' },
  { to: '/roles', icon: '🎭', label: '角色' },
  { to: '/collab', icon: '🤝', label: '协作组' },
  { to: '/skills', icon: '🧩', label: '技能' },
  { to: '/settings', icon: '⚙️', label: '设置' },
]

/**
 * 桌面端外壳：左侧栏（品牌 + 主导航 + 对话列表）+ 右侧内容区。
 *
 * 对话列表与移动端抽屉里那份同构 —— 导航和对话都在同一块，不再让聊天页自带第二条列表栏。
 * 导航的顺序、命名与行的样式（图标 · 文字 · 右侧箭头）也和移动端抽屉对齐；
 * 「智能体配置」「运行时」不在主导航里，收在「设置」页的配置入口（与移动端一致）。
 */
export default function App() {
  const [version, setVersion] = useState('')

  useEffect(() => {
    // 侧边栏只要一个版本号：走最轻的 /api/health，别让每次打开页面都去汇总系统信息。
    api
      .health()
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
              <span className="nav-chevron">›</span>
            </NavLink>
          ))}
        </nav>
        <ConversationList />
        <div className="sidebar-footer">
          配置智能体 → 创建角色 → 对话 / 协作
          {version && <div style={{ marginTop: 4 }}>版本 {version}</div>}
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ChatPage />} />
          <Route path="/collab" element={<CollabPage />} />
          <Route path="/collab/settings" element={<CollabPage />} />
          <Route path="/collab/settings/:teamId" element={<CollabPage />} />
          <Route path="/collab/runs/:runId" element={<CollabPage />} />
          <Route path="/roles" element={<RolesPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/runtimes" element={<RuntimesPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </main>
    </div>
  )
}
