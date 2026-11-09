import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import type { AgentInstance, AgentType, Role, Skill } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const AVATARS = ['🛠️', '🧪', '🔎', '🐳', '🔗', '🎨', '📚', '🧠', '⚡', '🛰️', '🧭', '🦾']

/** 角色列表：角色 = 人格设定 + 绑定的智能体；技能只能通过角色分配获得。 */
export function MobileRolesPage() {
  const navigate = useNavigate()
  const [roles, setRoles] = useState<Role[]>([])
  const [agents, setAgents] = useState<AgentInstance[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [agentTypes, setAgentTypes] = useState<AgentType[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Role | null>(null)
  const [creating, setCreating] = useState(false)

  const reload = useCallback(async () => {
    try {
      const [roleList, agentList, skillList, typeList] = await Promise.all([
        api.listRoles(),
        api.listAgents(),
        api.listSkills(),
        api.agentTypes(),
      ])
      setRoles(roleList)
      setAgents(agentList)
      setSkills(skillList)
      setAgentTypes(typeList)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const duplicate = async (role: Role) => {
    try {
      await api.duplicateRole(role.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const remove = async (role: Role) => {
    if (!confirm(`删除角色「${role.name}」？绑定它的对话会保留在历史里。`)) return
    try {
      await api.deleteRole(role.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  return (
    <div className="m-page">
      <MobileTopBar
        title="角色"
        subtitle="人格设定 + 绑定的智能体"
        actions={
          <button
            className="m-icon-btn"
            onClick={() => setCreating(true)}
            disabled={agents.length === 0}
            aria-label="新建角色"
          >
            ＋
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}

          {agents.length === 0 ? (
            <div className="banner info">
              还没有配置任何智能体。角色必须绑定一个智能体，请先到「设置 → 智能体配置」创建。
              <div style={{ marginTop: 8 }}>
                <button className="btn sm" onClick={() => navigate('/agents')}>
                  去配置智能体
                </button>
              </div>
            </div>
          ) : roles.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">🎭</div>
              <p>还没有角色</p>
              <button className="btn primary" onClick={() => setCreating(true)}>
                ＋ 新建角色
              </button>
            </div>
          ) : (
            <div className="m-list">
              {roles.map((role) => (
                <div className="card" key={role.id}>
                  <div className="row">
                    <span className="avatar">{role.avatar}</span>
                    <div className="m-item-main">
                      <div className="m-item-title">{role.name}</div>
                      <div className="m-item-sub">
                        {role.agent_name} · {role.tagline ?? '无备注'}
                      </div>
                    </div>
                    {role.skills.length > 0 && <span className="pill">{role.skills.length} 技能</span>}
                  </div>
                  <div className="m-item-actions" style={{ marginTop: 0 }}>
                    <button className="btn sm" onClick={() => setEditing(role)}>
                      编辑
                    </button>
                    <button className="btn sm" onClick={() => void duplicate(role)}>
                      复制
                    </button>
                    <button className="btn sm danger" onClick={() => void remove(role)}>
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {(creating || editing) && (
        <MobileSheet
          full
          title={editing ? `编辑角色 · ${editing.name}` : '新建角色'}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
        >
          <RoleForm
            role={editing}
            agents={agents}
            skills={skills}
            agentTypes={agentTypes}
            onSaved={async () => {
              setCreating(false)
              setEditing(null)
              await reload()
            }}
          />
        </MobileSheet>
      )}
    </div>
  )
}

function RoleForm({
  role,
  agents,
  skills,
  agentTypes,
  onSaved,
}: {
  role: Role | null
  agents: AgentInstance[]
  skills: Skill[]
  agentTypes: AgentType[]
  onSaved: () => void
}) {
  const [name, setName] = useState(role?.name ?? '')
  const [avatar, setAvatar] = useState(role?.avatar ?? AVATARS[0])
  const [tagline, setTagline] = useState(role?.tagline ?? '')
  const [agentId, setAgentId] = useState(role?.agent_id ?? agents[0]?.id ?? '')
  const [systemPrompt, setSystemPrompt] = useState(role?.system_prompt ?? '')
  const [workspace, setWorkspace] = useState(role?.workspace_dir ?? '')
  const [skillIds, setSkillIds] = useState<string[]>(role?.skill_ids ?? [])
  const [modelOverride, setModelOverride] = useState(String((role?.parameters?.model as string) ?? ''))
  const [gatewayOverride, setGatewayOverride] = useState(String((role?.parameters?.gateway_url as string) ?? ''))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const selected = agents.find((agent) => agent.id === agentId)
  const selectedType = agentTypes.find((type) => type.id === selected?.type_id)
  const supportsSkills = selectedType?.capabilities.skills ?? false
  const matchingSkills = skills.filter((skill) => skill.type_id === selected?.type_id)

  const toggleSkill = (skillId: string) =>
    setSkillIds((current) =>
      current.includes(skillId) ? current.filter((id) => id !== skillId) : [...current, skillId],
    )

  const save = async () => {
    if (!name.trim()) return setError('请填写角色名称')
    if (!agentId) return setError('请选择要绑定的智能体')
    setSaving(true)
    setError(null)

    const parameters: Record<string, unknown> = {}
    if (modelOverride.trim()) parameters.model = modelOverride.trim()
    if (gatewayOverride.trim()) parameters.gateway_url = gatewayOverride.trim()

    const payload = {
      name: name.trim(),
      avatar,
      tagline: tagline.trim() || null,
      agent_id: agentId,
      system_prompt: systemPrompt,
      workspace_dir: workspace.trim() || null,
      parameters,
    }
    try {
      // 技能分配紧随角色保存：未分配的技能对这次运行不生效。
      const saved = role ? await api.updateRole(role.id, payload) : await api.createRole(payload)
      await api.setRoleSkills(saved.id, skillIds)
      onSaved()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      {error && <div className="banner err">{error}</div>}

      <div className="field">
        <label className="field-label">角色名</label>
        <input
          className="input"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="例如 代码审查员"
        />
      </div>

      <div className="field">
        <label className="field-label">头像</label>
        <div className="row wrap" style={{ gap: 6 }}>
          {AVATARS.map((item) => (
            <button
              key={item}
              type="button"
              className={`btn sm${item === avatar ? ' primary' : ''}`}
              onClick={() => setAvatar(item)}
            >
              {item}
            </button>
          ))}
        </div>
      </div>

      <div className="field">
        <label className="field-label">备注</label>
        <input
          className="input"
          value={tagline}
          onChange={(event) => setTagline(event.target.value)}
          placeholder="一句话说明这个角色做什么"
        />
      </div>

      <div className="field">
        <label className="field-label">绑定智能体</label>
        <select className="select" value={agentId} onChange={(event) => setAgentId(event.target.value)}>
          {agents.map((agent) => (
            <option key={agent.id} value={agent.id}>
              {agent.name}（{agent.type_id}）
            </option>
          ))}
        </select>
        <span className="field-help">角色改绑智能体后，历史对话仍按原先快照的智能体复现。</span>
      </div>

      <div className="field">
        <label className="field-label">系统提示词</label>
        <textarea
          className="textarea"
          value={systemPrompt}
          onChange={(event) => setSystemPrompt(event.target.value)}
          placeholder="描述这个角色的人格、工作方式与输出要求"
        />
      </div>

      <div className="field">
        <label className="field-label">工作目录</label>
        <input
          className="input mono"
          value={workspace}
          onChange={(event) => setWorkspace(event.target.value)}
          placeholder="留空则由后端按对话分配"
        />
        <span className="field-help">留空时后端会为每个对话分配 data/workspaces/&lt;对话 ID&gt;。</span>
      </div>

      <div className="field">
        <label className="field-label">
          技能<span className="dim small">已分配 {skillIds.length} 个</span>
        </label>
        {!supportsSkills ? (
          <div className="banner info">该智能体的内核不支持技能，这个角色不会加载任何技能。</div>
        ) : matchingSkills.length === 0 ? (
          <div className="banner info">还没有属于这个内核的技能，请到「技能」页重新扫描。</div>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            {matchingSkills.map((skill) => (
              <label key={skill.id} className="skill-option">
                <input
                  type="checkbox"
                  checked={skillIds.includes(skill.id)}
                  onChange={() => toggleSkill(skill.id)}
                />
                <span className="skill-option-body">
                  <span className="skill-option-name">
                    {skill.name}
                    {!skill.available && <span className="pill err">目录已消失</span>}
                  </span>
                  <span className="dim small">{skill.description || '（无描述）'}</span>
                </span>
              </label>
            ))}
          </div>
        )}
        <span className="field-help">未勾选的技能不会被该角色加载。</span>
      </div>

      <div className="field">
        <label className="field-label">覆盖模型（可选）</label>
        <input
          className="input mono"
          value={modelOverride}
          onChange={(event) => setModelOverride(event.target.value)}
          placeholder="例如 opus / deepseek-reasoner"
        />
      </div>

      <div className="field">
        <label className="field-label">覆盖网关（可选）</label>
        <input
          className="input mono"
          value={gatewayOverride}
          onChange={(event) => setGatewayOverride(event.target.value)}
          placeholder="仅本角色生效"
        />
      </div>

      <button className="btn primary block" onClick={() => void save()} disabled={saving}>
        {saving ? '保存中…' : '保存角色'}
      </button>
    </>
  )
}
