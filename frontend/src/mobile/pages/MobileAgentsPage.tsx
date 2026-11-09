import { useCallback, useEffect, useMemo, useState } from 'react'
import { DynamicForm } from '../../components/DynamicForm'
import { api } from '../../lib/api'
import type { AgentInstance, AgentType, TestResult } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/** 智能体配置：适配器类型 + 网关 + 密钥 + 模型的一个实例，可被多个角色复用。 */
export function MobileAgentsPage() {
  const [types, setTypes] = useState<AgentType[]>([])
  const [agents, setAgents] = useState<AgentInstance[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<AgentInstance | null>(null)
  const [creating, setCreating] = useState(false)
  const [testing, setTesting] = useState<{ agent: AgentInstance; result: TestResult | null; busy: boolean } | null>(null)

  const reload = useCallback(async () => {
    try {
      const [typeList, agentList] = await Promise.all([api.agentTypes(), api.listAgents()])
      setTypes(typeList)
      setAgents(agentList)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const runTest = async (agent: AgentInstance) => {
    setTesting({ agent, result: null, busy: true })
    try {
      const result = await api.testAgent(agent.id)
      setTesting({ agent, result, busy: false })
    } catch (e) {
      setTesting({ agent, result: { ok: false, message: describeError(e), checks: [] }, busy: false })
    }
  }

  const duplicate = async (agent: AgentInstance) => {
    try {
      await api.duplicateAgent(agent.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const remove = async (agent: AgentInstance) => {
    if (!confirm(`删除智能体「${agent.name}」？被角色引用时会拒绝删除。`)) return
    try {
      await api.deleteAgent(agent.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  return (
    <div className="m-page">
      <MobileTopBar
        title="智能体配置"
        subtitle={`${types.length} 种适配器 · ${agents.length} 个实例`}
        actions={
          <button className="m-icon-btn" onClick={() => setCreating(true)} aria-label="新建智能体">
            ＋
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}

          {agents.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">⚙️</div>
              <p>还没有配置智能体</p>
              <button className="btn primary" onClick={() => setCreating(true)}>
                ＋ 新建智能体
              </button>
            </div>
          ) : (
            <div className="m-list">
              {agents.map((agent) => {
                const availability = agent.availability
                return (
                  <div className="card" key={agent.id}>
                    <div className="row">
                      <div className="m-item-main">
                        <div className="m-item-title">{agent.name}</div>
                        <div className="m-item-sub">
                          {agent.type_name ?? agent.type_id}
                          {agent.model ? ` · ${agent.model}` : ''} · {agent.runtime ?? '—'}
                        </div>
                      </div>
                      {availability && (
                        <span className={`pill${availability.installed ? ' ok' : ' err'}`}>
                          {availability.installed ? '可用' : '未就绪'}
                        </span>
                      )}
                    </div>
                    {availability?.detail && <p className="dim small">{availability.detail}</p>}
                    <div className="row wrap" style={{ gap: 8 }}>
                      {agent.api_key_set && <span className="pill">密钥 {agent.api_key_preview}</span>}
                      {agent.gateway_url && <span className="pill">{agent.gateway_url}</span>}
                    </div>
                    <div className="m-item-actions" style={{ marginTop: 0 }}>
                      <button className="btn sm" onClick={() => setEditing(agent)}>
                        编辑
                      </button>
                      <button className="btn sm" onClick={() => void runTest(agent)}>
                        连通性测试
                      </button>
                      <button className="btn sm" onClick={() => void duplicate(agent)}>
                        复制
                      </button>
                      <button className="btn sm danger" onClick={() => void remove(agent)}>
                        删除
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>

      {(creating || editing) && (
        <MobileSheet
          full
          title={editing ? `编辑智能体 · ${editing.name}` : '新建智能体'}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
        >
          <AgentForm
            agent={editing}
            types={types}
            onSaved={async () => {
              setCreating(false)
              setEditing(null)
              await reload()
            }}
          />
        </MobileSheet>
      )}

      {testing && (
        <MobileSheet title={`连通性测试 · ${testing.agent.name}`} onClose={() => setTesting(null)}>
          {testing.busy ? (
            <p className="dim small">正在检测…</p>
          ) : (
            <div className="stack" style={{ gap: 10 }}>
              <div className={`banner${testing.result?.ok ? ' info' : ' err'}`}>{testing.result?.message}</div>
              {testing.result?.checks.map((check) => (
                <div className="m-item" key={check.name} style={{ cursor: 'default' }}>
                  <span>{check.ok ? '✅' : '❌'}</span>
                  <span className="m-item-main">
                    <span className="m-item-title">{check.name}</span>
                    <span className="m-item-sub">{check.detail}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </MobileSheet>
      )}
    </div>
  )
}

function AgentForm({
  agent,
  types,
  onSaved,
}: {
  agent: AgentInstance | null
  types: AgentType[]
  onSaved: () => void
}) {
  const [typeId, setTypeId] = useState(agent?.type_id ?? '')
  const [name, setName] = useState(agent?.name ?? '')
  const [description, setDescription] = useState(agent?.description ?? '')
  const [gateway, setGateway] = useState(agent?.gateway_url ?? '')
  const [model, setModel] = useState(agent?.model ?? '')
  const [apiKey, setApiKey] = useState('')
  const [options, setOptions] = useState<Record<string, unknown>>(() => ({ ...(agent?.options ?? {}) }))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const selected = useMemo(() => types.find((type) => type.id === typeId) ?? null, [types, typeId])

  // 新建时选定类型：优先带入该智能体的本机默认配置，再退回适配器占位值；
  // 编辑已有实例时保持它自己保存过的配置不动。
  const pickType = (type: AgentType) => {
    setTypeId(type.id)
    if (agent) return
    const defaults = type.system_defaults
    setName((prev) => prev || type.name)
    setGateway(defaults?.gateway_url ?? type.default_gateway ?? '')
    setModel(defaults?.model ?? type.default_model ?? '')
    setOptions(
      Object.fromEntries(
        type.config_fields.map((field) => [field.key, defaults?.options?.[field.key] ?? field.default]),
      ),
    )
  }

  const save = async () => {
    if (!selected) return setError('请先选择智能体类型')
    if (!name.trim()) return setError('请填写名称')
    setSaving(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        name: name.trim(),
        description: description.trim() || null,
        gateway_url: gateway.trim() || null,
        model: model.trim() || null,
        options,
      }
      if (agent) {
        if (apiKey.trim()) payload.api_key = apiKey.trim()
        await api.updateAgent(agent.id, payload)
      } else {
        await api.createAgent({ ...payload, type_id: selected.id, api_key: apiKey.trim() || null })
      }
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

      {!agent && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="card-section-title">适配器类型</div>
          {types.map((type) => (
            <div
              key={type.id}
              className={`agent-type-row${type.id === typeId ? ' selected' : ''}${type.availability.installed ? '' : ' disabled'}`}
              onClick={() => pickType(type)}
            >
              <div className="m-item-main">
                <div className="agent-type-name">{type.name}</div>
                <div className="agent-type-desc">{type.description}</div>
              </div>
              <span className={`pill${type.availability.installed ? ' ok' : ' err'}`}>
                {type.availability.installed ? '可用' : '未就绪'}
              </span>
            </div>
          ))}
        </div>
      )}

      {!agent && selected?.system_defaults && <SystemDefaultsHint type={selected} />}

      <div className="field">
        <label className="field-label">名称</label>
        <input className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 我的 Codex" />
      </div>

      <div className="field">
        <label className="field-label">网关地址</label>
        <input
          className="input mono"
          value={gateway}
          onChange={(event) => setGateway(event.target.value)}
          placeholder={selected?.default_gateway ?? 'https://...'}
        />
      </div>

      <div className="field">
        <label className="field-label">模型</label>
        <input
          className="input mono"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          placeholder={selected?.default_model ?? ''}
        />
      </div>

      <div className="field">
        <label className="field-label">
          API 密钥
          {selected?.api_key_env && <span className="mono-sm dim">→ {selected.api_key_env}</span>}
        </label>
        <input
          className="input mono"
          type="password"
          autoComplete="new-password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder={agent?.api_key_set ? `已保存 ${agent.api_key_preview ?? ''} · 留空则不修改` : 'sk-...'}
        />
        <span className="field-help">密钥经 Fernet 加密存储，接口只返回掩码，绝不回传明文。</span>
      </div>

      <div className="field">
        <label className="field-label">备注</label>
        <input className="input" value={description} onChange={(event) => setDescription(event.target.value)} />
      </div>

      {selected && (
        <div className="stack">
          <div className="card-section-title">适配器设置</div>
          <DynamicForm fields={selected.config_fields} value={options} onChange={setOptions} />
        </div>
      )}

      <button className="btn primary block" onClick={() => void save()} disabled={saving}>
        {saving ? '保存中…' : '保存智能体'}
      </button>
    </>
  )
}

/** 新建实例时提示已读取到该内核在本机的真实配置（具体读哪个文件是适配器的知识）。 */
function SystemDefaultsHint({ type }: { type: AgentType }) {
  const defaults = type.system_defaults
  if (!defaults) return null
  const parts = [
    defaults.model ? `模型 ${defaults.model}` : null,
    defaults.gateway_url ? `网关 ${defaults.gateway_url}` : null,
    defaults.api_key_set ? `凭据 ${defaults.api_key_preview ?? '已配置'}` : null,
  ].filter(Boolean)
  if (parts.length === 0) return null
  return <div className="banner info">已读取该智能体的本机默认配置：{parts.join(' · ')}。</div>
}
