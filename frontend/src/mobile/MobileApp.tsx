import { useEffect, useState } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { api } from '../lib/api'
import { MobileTabBar } from './components/MobileTabBar'
import { MobileAgentsPage } from './pages/MobileAgentsPage'
import { MobileChatPage } from './pages/MobileChatPage'
import { MobileCollabPage } from './pages/MobileCollabPage'
import { MobileCollabRunPage } from './pages/MobileCollabRunPage'
import { MobileCollabTeamPage } from './pages/MobileCollabTeamPage'
import { MobileConversationsPage } from './pages/MobileConversationsPage'
import { MobileRolesPage } from './pages/MobileRolesPage'
import { MobileSettingsPage } from './pages/MobileSettingsPage'
import { MobileSkillsPage } from './pages/MobileSkillsPage'

/**
 * 移动端应用：`/m/` 这条链接的独立入口。
 *
 * 与桌面端的 `App.tsx` 物理分离（各自的 HTML、入口、页面与样式表），
 * 只共享与界面无关的基础设施：`lib/api`、`lib/types`、`lib/agentStream` 等。
 */
export function MobileApp() {
  const [version, setVersion] = useState('')

  useEffect(() => {
    api
      .overview()
      .then((info) => setVersion(info.version))
      .catch(() => undefined)
  }, [])

  return (
    <Routes>
      <Route element={<MobileLayout />}>
        <Route path="/" element={<Navigate to="/chat" replace />} />
        <Route path="/chat" element={<MobileConversationsPage version={version} />} />
        <Route path="/chat/:conversationId" element={<MobileChatPage />} />
        <Route path="/roles" element={<MobileRolesPage />} />
        <Route path="/agents" element={<MobileAgentsPage />} />
        <Route path="/skills" element={<MobileSkillsPage />} />
        <Route path="/collab" element={<MobileCollabPage />} />
        <Route path="/collab/teams/:teamId" element={<MobileCollabTeamPage />} />
        <Route path="/collab/runs/:runId" element={<MobileCollabRunPage />} />
        <Route path="/settings" element={<MobileSettingsPage version={version} />} />
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Route>
    </Routes>
  )
}

/** 外壳：内容区 + 底部 Tab；对话详情页沉浸式，不显示 Tab。 */
function MobileLayout() {
  const { pathname } = useLocation()
  const immersive = /^\/chat\/[^/]+/.test(pathname)
  return (
    <div className="m-app">
      <div className="m-main">
        <Outlet />
      </div>
      {!immersive && <MobileTabBar />}
    </div>
  )
}
