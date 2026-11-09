import { useEffect, useState } from 'react'
import { Modal } from '../components/Modal'
import { api, ApiError } from '../lib/api'
import type { AgentInstance, AgentType, Role, Skill } from '../lib/types'

const AVATARS = ['🛠️', '🧪', '🔎', '🐳', '🔗', '🎨', '📚', '🧠', '⚡', '🛰️', '🧭', '🦾']
const PROMPT_TEMPLATES: { label: string; body: string }[] = [
  {
    label: '严格代码审查',
    body: '你是一名严格的资深工程师。先阅读仓库再动手，指出风险与权衡，只提交最小且可验证的改动，并说明如何验证。',
  },
  {
    label: '结对的编程搭子',
    body: '你是务实的结对编程伙伴。改动小步提交，每次修改后运行相关测试，用一两句话总结改动与后续建议。',
  },
  {
    label: '调研助手',
    body: '你是研究型助手。先收集证据，引用你读过的文件路径，明确区分「事实」与「推测」。',
  },
]

export function RolesPage() {
  const [roles, setRoles] = useState<Role[]>([])
  const [agents, setAgents] = useState<AgentInstance[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [agentTypes, setAgentTypes] = useState<AgentType[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<Role | null>(null)
  const [creating, setCreating] = useState(false)

  const reload = async () => {
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
      setError(describe(e))
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">角色</h1>
          <p className="page-desc">
            角色 = 一个人格设定（系统提示词）+ 一个已配置的智能体。同一个智能体可以驱动多个不同角色。
          </p>
        </div>
        <div className="spacer" />
        <button className="btn primary" onClick={() => setCreating(true)} disabled={agents.length === 0}>
          + 新建角色
        </button>
      </header>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}
        {agents.length === 0 && (
          <div className="banner info">请先在「智能体配置」页创建至少一个智能体，然后再回来创建角色。</div>
        )}

        {roles.length === 0 ? (
          <div className="empty">
            <div className="empty-icon">🎭</div>
            <p>还没有角色。</p>
          </div>
        ) : (
          <div className="grid">
            {roles.map((role) => (
              <article className="card" key={role.id}>
                <div className="card-head">
                  <div className="avatar" style={{ width: 40, height: 40, fontSize: 19 }}>
                    {role.avatar}
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div className="card-title">{role.name}</div>
                    <div className="card-sub">{role.tagline ?? '无备注'}</div>
                  </div>
                </div>
                <div className="chips">
                  <span className="pill brand">{role.agent_name}</span>
                  <span className="pill mono-sm">{role.agent_type_id}</span>
                  {role.skills.length > 0 && (
                    <span className="pill ok">🧩 {role.skills.length} 个技能</span>
                  )}
                </div>
                <div className="bubble-body" style={{ maxHeight: 96, overflow: 'hidden', fontSize: 12.5 }}>
                  {role.system_prompt || '（未设置系统提示词）'}
                </div>
                <div className="card-actions" style={{ marginLeft: 0 }}>
                  <button className="btn sm" onClick={() => setEditing(role)}>
                    编辑
                  </button>
                  <button
                    className="btn sm"
                    onClick={async () => {
                      await api.duplicateRole(role.id)
                      await reload()
                    }}
                  >
                    复制
                  </button>
                  <button
                    className="btn sm danger"
                    onClick={async () => {
                      if (!confirm(`删除角色「${role.name}」？已有对话会保留在历史里。`)) return
                      try {
                        await api.deleteRole(role.id)
                        await reload()
                      } catch (e) {
                        setError(describe(e))
                      }
                    }}
                  >
                    删除
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      {(creating || editing) && (
        <RoleModal
          role={editing}
          agents={agents}
          skills={skills}
          agentTypes={agentTypes}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={async () => {
            setCreating(false)
            setEditing(null)
            await reload()
          }}
        />
      )}
    </>
  )
}

function RoleModal({
  role,
  agents,
  skills,
  agentTypes,
  onClose,
  onSaved,
}: {
  role: Role | null
  agents: AgentInstance[]
  skills: Skill[]
  agentTypes: AgentType[]
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = useState(role?.name ?? '')
  const [avatar, setAvatar] = useState(role?.avatar ?? '🛠️')
  const [tagline, setTagline] = useState(role?.tagline ?? '')
  const [agentId, setAgentId] = useState(role?.agent_id ?? agents[0]?.id ?? '')
  const [systemPrompt, setSystemPrompt] = useState(role?.system_prompt ?? '')
  const [workspace, setWorkspace] = useState(role?.workspace_dir ?? '')
  const [skillIds, setSkillIds] = useState<string[]>(role?.skill_ids ?? [])
  const [modelOverride, setModelOverride] = useState(String((role?.parameters?.model as string) ?? ''))
  const [gatewayOverride, setGatewayOverride] = useState(String((role?.parameters?.gateway_url as string) ?? ''))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
      // 技能分配紧随角色保存：技能只能通过分配获得，未分配即为不生效。
      const saved = role ? await api.updateRole(role.id, payload) : await api.createRole(payload)
      await api.setRoleSkills(saved.id, skillIds)
      onSaved()
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  const selected = agents.find((a) => a.id === agentId)
  const selectedType = agentTypes.find((type) => type.id === selected?.type_id)
  const supportsSkills = selectedType?.capabilities.skills ?? false
  const matchingSkills = skills.filter((skill) => skill.type_id === selected?.type_id)
  const otherSkills = skills.filter((skill) => skill.type_id !== selected?.type_id)
  const toggleSkill = (skillId: string) =>
    setSkillIds((current) =>
      current.includes(skillId) ? current.filter((id) => id !== skillId) : [...current, skillId],
    )

  return (
    <Modal
      wide
      title={role ? `编辑角色 · ${role.name}` : '新建角色'}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={() => void save()} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error && <div className="banner err">{error}</div>}

      <div className="form-grid">
        <div className="field">
          <label className="field-label">角色名</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 代码审查员" />
        </div>
        <div className="field">
          <label className="field-label">头像</label>
          <div className="chips">
            {AVATARS.map((emoji) => (
              <button
                key={emoji}
                type="button"
                className={emoji === avatar ? 'btn sm primary' : 'btn sm'}
                style={{ padding: '5px 9px', fontSize: 15 }}
                onClick={() => setAvatar(emoji)}
              >
                {emoji}
              </button>
            ))}
          </div>
        </div>

        <div className="field span-2">
          <label className="field-label">绑定智能体</label>
          <select
            className="select"
            value={agentId}
            onChange={(e) => {
              const next = e.target.value
              setAgentId(next)
              const agent = agents.find((a) => a.id === next)
              const nextType = agentTypes.find((type) => type.id === agent?.type_id)
              setSkillIds((current) =>
                current.filter(
                  (id) => skills.find((skill) => skill.id === id)?.type_id === nextType?.id,
                ),
              )
            }}
          >
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.name} · {agent.type_name ?? agent.type_id}
                {agent.enabled ? '' : '（已停用）'}
              </option>
            ))}
          </select>
          {selected && (
            <span className="field-help">
              网关：{selected.gateway_url ?? '默认'} · 模型：{selected.model ?? '默认'}
              {selected.availability && !selected.availability.installed ? ' · ⚠️ 该智能体的运行时未安装' : ''}
            </span>
          )}
        </div>

        <div className="field span-2">
          <label className="field-label">一句话简介</label>
          <input className="input" value={tagline} onChange={(e) => setTagline(e.target.value)} placeholder="显示在对话列表里" />
        </div>

        <div className="field span-2">
          <label className="field-label">
            系统提示词
            <span className="dim small" style={{ marginLeft: 'auto' }}>
              <button className="btn ghost sm" type="button" onClick={() => setSystemPrompt('')}>
                清空
              </button>
            </span>
          </label>
          <textarea
            className="textarea"
            style={{ minHeight: 130 }}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="描述这个角色的身份、风格与工作方式…"
          />
          <div className="chips">
            {PROMPT_TEMPLATES.map((template) => (
              <button
                key={template.label}
                type="button"
                className="btn sm"
                onClick={() => setSystemPrompt(template.body)}
              >
                {template.label}
              </button>
            ))}
          </div>
        </div>

        <div className="field span-2">
          <label className="field-label">工作目录</label>
          <input
            className="input mono"
            value={workspace}
            onChange={(e) => setWorkspace(e.target.value)}
            placeholder="留空则使用每个对话独立的沙箱目录"
          />
          <span className="field-help">
            智能体将以该目录为工作目录运行。留空时，后端会为每个对话分配 <code className="mono-sm">data/workspaces/&lt;对话 ID&gt;</code>。
          </span>
        </div>

        <div className="field span-2">
          <label className="field-label">
            技能
            <span className="dim small" style={{ marginLeft: 'auto' }}>
              已分配 {skillIds.length} 个
            </span>
          </label>
          {!supportsSkills ? (
            <div className="banner info">该智能体的内核不支持技能，这个角色不会加载任何技能。</div>
          ) : matchingSkills.length === 0 ? (
            <div className="banner info">
              还没有属于「{selectedType?.name ?? selected?.type_id}」的技能。请到「技能」页重新扫描，或换一个支持技能的内核。
            </div>
          ) : (
            <div className="skill-picker">
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
          <span className="field-help">
            角色能用的技能只能来自这里的分配：未勾选的技能不会被该角色加载。改绑到其它类型的智能体时，不匹配的技能会被自动移除。
          </span>
          {otherSkills.length > 0 && (
            <span className="field-help">
              另有 {otherSkills.length} 个技能来自其它智能体内核，与本角色不匹配，无法选择。
            </span>
          )}
        </div>

        <div className="field">
          <label className="field-label">覆盖模型（可选）</label>
          <input className="input mono" value={modelOverride} onChange={(e) => setModelOverride(e.target.value)} placeholder="例如 opus / deepseek-reasoner" />
        </div>
        <div className="field">
          <label className="field-label">覆盖网关（可选）</label>
          <input className="input mono" value={gatewayOverride} onChange={(e) => setGatewayOverride(e.target.value)} placeholder="仅本角色生效" />
        </div>
      </div>
    </Modal>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
