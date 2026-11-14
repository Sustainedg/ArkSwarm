/**
 * 「协作」页：只保留协作运行 —— 发起、观察、干预。
 *
 * 协作组的可视化编排（原来这一页的「协作设置」页签）已经移到「智能体 → 协作组」：
 * 协作组本质上是「哪些智能体、以什么岗位连成一张图」，和智能体配置放在一起更顺。
 * 老地址 `/collab/settings*` 在 `App.tsx` 里重定向过去，书签不会失效。
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { CollabRunsPanel } from '../components/CollabRunsPanel'
import { api, ApiError } from '../lib/api'
import type { CollabTeam, AgentProfile } from '../lib/types'

export function CollabPage() {
  const navigate = useNavigate()
  const { runId } = useParams<{ runId?: string }>()

  const [teams, setTeams] = useState<CollabTeam[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const [teamList, agentList] = await Promise.all([
        api.listCollabTeams(),
        api.listAgents(),
      ])
      setTeams(teamList)
      setAgents(agentList)
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
      {error && (
        <div style={{ padding: '12px 26px 0' }}>
          <div className="banner err">{error}</div>
        </div>
      )}

      <CollabRunsPanel
        runId={runId}
        teams={teams}
        agents={agents}
        onOpenRun={(id) => navigate(id ? `/collab/runs/${id}` : '/collab')}
      />
    </>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
