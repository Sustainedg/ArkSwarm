/**
 * 「协作」页：一个菜单 + 页内两个页签（评审结论：保持左侧主导航清爽）。
 *
 * * `/collab`：协作运行 —— 发起、观察、干预（M1 的后端引擎在这里被真正用起来）
 * * `/collab/settings`：协作设置 —— 可视化编排（画布 + 属性面板）
 */
import { useCallback, useEffect, useState } from 'react'
import { NavLink, useLocation, useNavigate, useParams } from 'react-router-dom'
import { CollabRunsPanel } from '../components/CollabRunsPanel'
import { CollabSettingsPanel } from '../components/CollabSettingsPanel'
import { api, ApiError } from '../lib/api'
import type { AgentType, CollabTeam, AgentProfile } from '../lib/types'

export function CollabPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const { runId, teamId } = useParams<{ runId?: string; teamId?: string }>()
  const settingsTab = location.pathname.startsWith('/collab/settings')

  const [teams, setTeams] = useState<CollabTeam[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [agentTypes, setAgentTypes] = useState<AgentType[]>([])
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const [teamList, agentList, typeList] = await Promise.all([
        api.listCollabTeams(),
        api.listAgents(),
        api.agentTypes(),
      ])
      setTeams(teamList)
      setAgents(agentList)
      setAgentTypes(typeList)
      setError(null)
    } catch (e) {
      setError(describe(e))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">协作</h1>
          <p className="page-desc">
            把多个智能体连成一张图：它们之间只用「工单」交接任务，串行推进，审查不过就带意见打回返工。
          </p>
        </div>
        <div className="spacer" />
        <nav className="tabs">
          <NavLink to="/collab" end className={({ isActive }) => `tab${isActive ? ' active' : ''}`}>
            协作运行
          </NavLink>
          <NavLink to="/collab/settings" className={({ isActive }) => `tab${isActive ? ' active' : ''}`}>
            协作设置
          </NavLink>
        </nav>
      </header>

      {error && (
        <div style={{ padding: '12px 26px 0' }}>
          <div className="banner err">{error}</div>
        </div>
      )}

      {settingsTab ? (
        <CollabSettingsPanel
          teamId={teamId}
          teams={teams}
          agents={agents}
          agentTypes={agentTypes}
          onOpenTeam={(id) => navigate(id ? `/collab/settings/${id}` : '/collab/settings')}
          onTeamsChanged={reload}
        />
      ) : (
        <CollabRunsPanel
          runId={runId}
          teams={teams}
          agents={agents}
          onOpenRun={(id) => navigate(id ? `/collab/runs/${id}` : '/collab')}
        />
      )}
    </>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
