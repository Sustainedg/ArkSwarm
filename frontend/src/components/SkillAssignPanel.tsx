import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { AgentProfile, SkillDetail } from '../lib/types'
import { AgentAvatar } from './AgentAvatar'

/**
 * 「把这个技能分配给哪些智能体」的勾选面板（两端共用）。
 *
 * 勾选即生效：每次点击都调用「整表替换智能体技能分配」的接口并回调 `onChanged`，
 * 不像智能体表单那样等保存 —— 这里的入口是「从技能出发挑智能体」。
 * 只有与技能同内核的智能体可以勾选（运行层也按这条规则兜底）。
 */
export function SkillAssignPanel({
  skill,
  agents,
  onChanged,
}: {
  skill: SkillDetail
  agents: AgentProfile[]
  onChanged: () => Promise<void>
}) {
  const [picked, setPicked] = useState<string[]>(skill.agents.map((agent) => agent.id))
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const candidates = agents.filter((agent) => agent.type_id === skill.type_id)
  const others = agents.length - candidates.length

  const toggle = async (agent: AgentProfile) => {
    const on = !picked.includes(agent.id)
    const next = on
      ? [...new Set([...agent.skill_ids, skill.id])]
      : agent.skill_ids.filter((id) => id !== skill.id)
    setBusy(agent.id)
    setError(null)
    try {
      await api.setAgentSkills(agent.id, next)
      setPicked((prev) =>
        on ? [...new Set([...prev, agent.id])] : prev.filter((id) => id !== agent.id),
      )
      await onChanged()
    } catch (e) {
      setError(describe(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="stack" style={{ gap: 10 }}>
      {error && <div className="banner err">{error}</div>}
      <p className="dim small">
        勾选即生效：分配后这些智能体下一次运行就会加载这个技能，取消勾选则立即解除。
      </p>
      {candidates.length === 0 ? (
        <p className="dim small">
          还没有 {skill.type_name ?? skill.type_id} 内核的智能体可以分配。
        </p>
      ) : (
        <div className="skill-picker">
          {candidates.map((agent) => (
            <label key={agent.id} className="skill-option">
              <input
                type="checkbox"
                checked={picked.includes(agent.id)}
                disabled={busy === agent.id}
                onChange={() => void toggle(agent)}
              />
              <span className="skill-option-body">
                <span className="skill-option-name">
                  <AgentAvatar
                      avatar={agent.avatar}
                      avatarPreset={agent.avatar_preset}
                      avatarFile={agent.avatar_file}
                      agentId={agent.id}
                      size={16}
                      className="agent-avatar-inline"
                    /> {agent.name}
                </span>
                <span className="dim small">{agent.tagline || '（无备注）'}</span>
              </span>
            </label>
          ))}
        </div>
      )}
      {others > 0 && (
        <span className="field-help">另有 {others} 个智能体的内核与它不匹配，无法分配。</span>
      )}
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
