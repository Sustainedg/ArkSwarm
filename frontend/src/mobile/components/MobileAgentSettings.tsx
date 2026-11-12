import { useCallback, useEffect, useState } from 'react'
import { DynamicForm } from '../../components/DynamicForm'
import { api } from '../../lib/api'
import type { AgentInstance, AgentType, TestResult } from '../../lib/types'
import { describeError } from '../util'
import { MobileSheet } from './MobileSheet'

/**
 * 某个内核（适配器类型）的「设置」：这个内核下所有智能体实例的增删改与连通性测试。
 *
 * 它不是一个独立页面 —— 从「运行时」页每个内核卡片的「设置」进来，落在底部弹层里（`MobileSheet`）。
 * 一个内核 = 一份安装 + N 份配置（网关 / 密钥 / 模型 / 适配器选项），所以「装内核」和
 * 「配这个内核怎么跑」放在同一处，比拆成两个页面好找。
 */
export function MobileAgentSettings({ typeId, onChanged }: { typeId: string; onChanged?: () => void }) {
  const [types, setTypes] = useState<AgentType[]>([])
  const [agents, setAgents] = useState<AgentInstance[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState<AgentInstance | null>(null)
  const [creating, setCreating] = useState(false)
  const [testing, setTesting] = useState<{ agent: AgentInstance; result: TestResult | null; busy: boolean } | null>(
    null,
  )
  // 注：编辑弹层需要适配器类型（选项表单由它而来），类型没加载完时不弹 —— 本地请求很快，肉眼无感。

  const reload = useCallback(async () => {
    try {
      const [typeList, agentList] = await Promise.all([api.agentTypes(), api.listAgents()])
      setTypes(typeList)
      setAgents(agentList.filter((agent) => agent.type_id === typeId))
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [typeId])

  useEffect(() => {
    void reload()
  }, [reload])

  const type = types.find((item) => item.id === typeId) ?? null

  const refresh = async () => {
    await reload()
    onChanged?.()
  }

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
      await refresh()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const remove = async (agent: AgentInstance) => {
    if (!confirm(`删除智能体「${agent.name}」？被角色引用时会拒绝删除。`)) return
    try {
      await api.deleteAgent(agent.id)
      await refresh()
    } catch (e) {
      setError(describeError(e))
    }
  }

  return (
    <>
      {error && <div className="banner err">{error}</div>}

      {type && (
        <div className="card">
          <div className="m-item-main">
            <div className="m-item-title">{type.name}</div>
            <div className="m-item-sub">{type.description}</div>
          </div>
          <div className="row wrap" style={{ gap: 6 }}>
            <span className={`pill${type.availability.installed ? ' ok' : ' err'}`}>
              {type.availability.installed ? '可用' : '未就绪'}
            </span>
            <span className="pill">{agents.length} 个实例</span>
          </div>
          {type.availability.detail && <p className="dim small">{type.availability.detail}</p>}
        </div>
      )}

      <button className="btn primary block" onClick={() => setCreating(true)}>
        ＋ 新建实例
      </button>

      {loading ? (
        <p className="dim small">正在读取这个内核的配置…</p>
      ) : agents.length === 0 ? (
        <p className="dim small">这个内核还没有配置实例。新建一个，填好网关 / 密钥 / 模型就能被角色使用。</p>
      ) : (
        <div className="m-list">
          {agents.map((agent) => (
            <div className="card" key={agent.id}>
              <div className="row" style={{ gap: 8 }}>
                <div className="m-item-main">
                  <div className="m-item-title">{agent.name}</div>
                  <div className="m-item-sub">
                    {agent.model ? `${agent.model} · ` : ''}
                    {agent.runtime === 'cli' ? 'CLI' : '进程内 API'}
                  </div>
                </div>
                {agent.availability && (
                  <span className={`pill${agent.availability.installed ? ' ok' : ' err'}`}>
                    {agent.availability.installed ? '可用' : '未就绪'}
                  </span>
                )}
              </div>
              {agent.availability?.detail && <p className="dim small">{agent.availability.detail}</p>}
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
          ))}
        </div>
      )}

      {creating && type && (
        <MobileSheet
          full
          title={`新建 · ${type.name}`}
          onClose={() => setCreating(false)}
        >
          <AgentForm
            type={type}
            agent={null}
            onSaved={async () => {
              setCreating(false)
              await refresh()
            }}
          />
        </MobileSheet>
      )}

      {editing && type && (
        <MobileSheet full title={`编辑 · ${editing.name}`} onClose={() => setEditing(null)}>
          <AgentForm
            type={type}
            agent={editing}
            onSaved={async () => {
              setEditing(null)
              await refresh()
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
    </>
  )
}

function AgentForm({
  type,
  agent,
  onSaved,
}: {
  type: AgentType
  agent: AgentInstance | null
  onSaved: () => void
}) {
  const defaults = type.system_defaults
  const [name, setName] = useState(agent?.name ?? type.name)
  const [description, setDescription] = useState(agent?.description ?? '')
  const [gateway, setGateway] = useState(
    agent?.gateway_url ?? defaults?.gateway_url ?? type.default_gateway ?? '',
  )
  const [model, setModel] = useState(agent?.model ?? defaults?.model ?? type.default_model ?? '')
  const [apiKey, setApiKey] = useState('')
  const [options, setOptions] = useState<Record<string, unknown>>(() =>
    agent
      ? { ...agent.options }
      : Object.fromEntries(
          type.config_fields.map((field) => [field.key, defaults?.options?.[field.key] ?? field.default]),
        ),
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
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
        await api.createAgent({ ...payload, type_id: type.id, api_key: apiKey.trim() || null })
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
      {!agent && defaults && <SystemDefaultsHint type={type} />}

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
          placeholder={type.default_gateway ?? 'https://...'}
        />
      </div>

      <div className="field">
        <label className="field-label">模型</label>
        <input
          className="input mono"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          placeholder={type.default_model ?? ''}
        />
      </div>

      <div className="field">
        <label className="field-label">
          API 密钥
          {type.api_key_env && <span className="mono-sm dim">→ {type.api_key_env}</span>}
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

      {type.config_fields.length > 0 && (
        <div className="stack">
          <div className="card-section-title">适配器设置</div>
          <DynamicForm fields={type.config_fields} value={options} onChange={setOptions} />
        </div>
      )}

      <button className="btn primary block" onClick={() => void save()} disabled={saving}>
        {saving ? '保存中…' : '保存'}
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
