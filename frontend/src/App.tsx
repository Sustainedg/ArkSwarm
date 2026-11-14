import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useParams } from 'react-router-dom'
import { ConversationList } from './components/ConversationList'
import { api } from './lib/api'
import { ChatPage } from './pages/ChatPage'
import { CollabPage } from './pages/CollabPage'
import { AgentsPage } from './pages/AgentsPage'
import { McpPage } from './pages/McpPage'
import { SkillsPage } from './pages/SkillsPage'
import { SettingsPage } from './pages/SettingsPage'
import { UsagePage } from './pages/UsagePage'

//: 与移动端抽屉的主导航**同一份菜单**（顺序、命名、图标都一样）：
//: 「内核管理」「自定义网关」不在菜单里，收在「设置」页的标签页里（移动端是配置入口）。
const NAV = [
  { to: '/chat', icon: '💬', label: '对话' },
  { to: '/agents', icon: '🤖', label: '智能体' },
  { to: '/collab', icon: '🤝', label: '协作组' },
  { to: '/skills', icon: '🧩', label: '技能' },
  { to: '/mcp', icon: '🔌', label: 'MCP' },
  { to: '/usage', icon: '📊', label: '用量' },
  { to: '/settings', icon: '⚙️', label: '设置' },
]

/** 侧边栏是否折叠存在 localStorage：折叠是这台设备的个人偏好，跟主题一样只影响本机。 */
const SIDEBAR_KEY = 'magenta.sidebar'

function readSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_KEY) === 'collapsed'
  } catch {
    /* 隐私模式下 localStorage 可能不可用：默认展开 */
    return false
  }
}

/** 老的「协作设置」深链 → 智能体页的协作组标签，并尽量保留原来选中的那条协作组。 */
function CollabSettingsRedirect() {
  const { teamId } = useParams<{ teamId?: string }>()
  return (
    <Navigate
      to={teamId ? `/agents?tab=collab&team=${encodeURIComponent(teamId)}` : '/agents?tab=collab'}
      replace
    />
  )
}

/**
 * 桌面端外壳：左侧栏（品牌 + 主导航 + 对话列表）+ 右侧内容区。
 *
 * 对话列表与移动端抽屉里那份同构 —— 导航和对话都在同一块，不再让聊天页自带第二条列表栏。
 * 导航的顺序、命名与行的样式（图标 · 文字 · 右侧箭头）也和移动端抽屉对齐；
 * 「内核管理」「自定义网关」不在主导航里，收在「设置」页的标签页里（与移动端一致）。
 */
export default function App() {
  const [version, setVersion] = useState('')
  const [collapsed, setCollapsed] = useState(readSidebarCollapsed)

  const toggleSidebar = () =>
    setCollapsed((current) => {
      const next = !current
      try {
        localStorage.setItem(SIDEBAR_KEY, next ? 'collapsed' : 'open')
      } catch {
        /* 存不进去也不影响本次会话 */
      }
      return next
    })

  useEffect(() => {
    // 侧边栏只要一个版本号：走最轻的 /api/health，别让每次打开页面都去汇总系统信息。
    api
      .health()
      .then((info) => setVersion(info.version))
      .catch(() => undefined)
  }, [])

  return (
    <div className={`app-shell${collapsed ? ' collapsed' : ''}`}>
      <aside className="sidebar" id="app-sidebar">
        <div className="brand">
          <div className="brand-mark">✦</div>
          <div className="brand-text">
            <div className="brand-name">
              Magenta
              {version && <span className="brand-version">{version}</span>}
            </div>
            <div className="brand-sub">多智能体控制台</div>
          </div>
          <button
            type="button"
            className="sidebar-toggle"
            onClick={toggleSidebar}
            aria-expanded={!collapsed}
            aria-controls="app-sidebar"
            aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'}
            title={collapsed ? '展开侧边栏' : '折叠侧边栏'}
          >
            {collapsed ? '›' : '‹'}
          </button>
        </div>
        <nav className="stack" style={{ gap: 4 }}>
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}
              /* 折叠后只剩图标：鼠标停上去给名字 */
              title={collapsed ? item.label : undefined}
            >
              <span className="nav-icon">{item.icon}</span>
              <span className="nav-label">{item.label}</span>
              <span className="nav-chevron">›</span>
            </NavLink>
          ))}
        </nav>
        <ConversationList />
        <div className="sidebar-footer">
          配置网关与智能体 → 对话 / 协作
          {version && <div style={{ marginTop: 4 }}>版本 {version}</div>}
        </div>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ChatPage />} />
          <Route path="/collab" element={<CollabPage />} />
          {/* 协作组设置已移到「智能体 → 协作组」；老地址保留重定向，书签与外部链接不会失效。 */}
          <Route path="/collab/settings" element={<Navigate to="/agents?tab=collab" replace />} />
          <Route path="/collab/settings/:teamId" element={<CollabSettingsRedirect />} />
          <Route path="/collab/runs/:runId" element={<CollabPage />} />
          <Route path="/roles" element={<Navigate to="/agents" replace />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/mcp" element={<McpPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          {/* 内核管理已经收进「设置 → 内核管理」（以前这里有独立一页，还从设置里再链一次，
              等于同一个东西有两个入口）。老地址保留重定向，书签不会失效。 */}
          <Route path="/runtimes" element={<Navigate to="/settings?tab=kernels" replace />} />
          <Route path="/usage" element={<UsagePage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </main>
    </div>
  )
}
