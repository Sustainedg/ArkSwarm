import { useEffect, useMemo, useState } from 'react'
import { DynamicForm } from '../components/DynamicForm'
import { Modal } from '../components/Modal'
import { api, ApiError } from '../lib/api'
import type { AgentInstance, AgentType, TestResult } from '../lib/types'

export function AgentsPage() {
  const [types, setTypes] = useState<AgentType[]>([])
  const [agents, setAgents] = useState<AgentInstance[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const [editing, setEditing] = useState<AgentInstance | null>(null)
  const [creating, setCreating] = useState(false)
  const [testing, setTesting] = useState<{ agent: AgentInstance; result: TestResult | null; busy: boolean } | null>(null)

  const reload = async () => {
    setLoading(true)
    try {
      const [typeList, agentList] = await Promise.all([api.agentTypes(), api.listAgents()])
      setTypes(typeList)
      setAgents(agentList)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  const typeMap = useMemo(() => Object.fromEntries(types.map((t) => [t.id, t])), [types])

  const remove = async (agent: AgentInstance) => {
    if (!confirm(`删除智能体「${agent.name}」？`)) return
    try {
      await api.deleteAgent(agent.id)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  const runTest = async (agent: AgentInstance) => {
    setTesting({ agent, result: null, busy: true })
    try {
      const result = await api.testAgent(agent.id)
      setTesting({ agent, result, busy: false })
    } catch (e) {
      setTesting({ agent, result: { ok: false, message: describe(e), checks: [] }, busy: false })
    }
  }

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">智能体配置</h1>
          <p className="page-desc">
            每个智能体都是「适配器类型 + 自定义网关 + API 密钥 + 模型」的一个实例，可被多个角色复用。
          </p>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={() => void reload()} disabled={loading}>
          {loading ? <span className="spin">◌</span> : '↻'} 刷新
        </button>
        <button className="btn primary" onClick={() => setCreating(true)}>
          + 新建智能体
        </button>
      </header>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}

        <div className="banner info">
          支持的适配器：{types.map((t) => t.name).join(' · ')}。新增一种智能体只需在后端放入一个适配器模块，
          或直接用「自定义 CLI」声明式接入。
        </div>

        {agents.length === 0 && !loading ? (
          <div className="empty">
            <div className="empty-icon">⚙️</div>
            <p>还没有配置任何智能体。</p>
            <p className="dim small" style={{ marginTop: 6 }}>
              点击「新建智能体」选择 Claude Code / Codex / Gemini CLI 等类型。
            </p>
          </div>
        ) : (
          <div className="grid">
            {agents.map((agent) => {
              const type = typeMap[agent.type_id]
              const availability = agent.availability ?? type?.availability
              return (
                <article className="card" key={agent.id}>
                  <div className="card-head">
                    <div style={{ minWidth: 0 }}>
                      <div className="card-title">{agent.name}</div>
                      <div className="card-sub">
                        {type?.vendor ?? '未知类型'} · {type?.name ?? agent.type_id}
                      </div>
                    </div>
                    <div className="card-actions">
                      <span className={`pill ${agent.runtime === 'api' ? 'brand' : ''}`}>
                        {agent.runtime === 'api' ? '进程内 API' : '本地 CLI'}
                      </span>
                    </div>
                  </div>

                  <div className="chips">
                    {availability &&
                      (availability.installed ? (
                        <span className="pill ok">
                          <span className="dot" />
                          {availability.version ?? '就绪'}
                        </span>
                      ) : (
                        <span className="pill err">
                          <span className="dot off" />
                          未安装
                        </span>
                      ))}
                    {agent.enabled ? <span className="pill">已启用</span> : <span className="pill warn">已停用</span>}
                    {agent.api_key_set ? (
                      <span className="pill">🔑 {agent.api_key_preview}</span>
                    ) : (
                      <span className="pill warn">未配置 API 密钥</span>
                    )}
                  </div>

                  <dl className="kv">
                    <dt>网关</dt>
                    <dd title={agent.gateway_url ?? ''}>{agent.gateway_url ?? '—'}</dd>
                    <dt>模型</dt>
                    <dd>{agent.model ?? '—'}</dd>
                  </dl>

                  <div className="card-actions" style={{ marginLeft: 0 }}>
                    <button className="btn sm primary" onClick={() => void runTest(agent)}>
                      连通性测试
                    </button>
                    <button className="btn sm" onClick={() => setEditing(agent)}>
                      编辑
                    </button>
                    <button
                      className="btn sm"
                      onClick={async () => {
                        await api.duplicateAgent(agent.id)
                        await reload()
                      }}
                    >
                      复制
                    </button>
                    <button className="btn sm danger" onClick={() => void remove(agent)}>
                      删除
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
        )}
      </div>

      {(creating || editing) && (
        <AgentModal
          types={types}
          agent={editing}
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

      {testing && (
        <Modal
          title={`连通性测试 · ${testing.agent.name}`}
          onClose={() => setTesting(null)}
          footer={
            <button className="btn" onClick={() => setTesting(null)}>
              关闭
            </button>
          }
        >
          {testing.busy ? (
            <div className="row">
              <span className="spin">◌</span> 正在探测…
            </div>
          ) : (
            <div className="stack" style={{ gap: 10 }}>
              <div className={`banner ${testing.result?.ok ? 'ok' : 'err'}`}>
                {testing.result?.ok ? '✅ 配置可用' : '❌ 存在问题'}：{testing.result?.message}
              </div>
              {testing.result?.checks.map((check) => (
                <div className="row" key={check.name}>
                  <span className={`pill ${check.ok ? 'ok' : 'err'}`}>{check.ok ? '通过' : '失败'}</span>
                  <span className="muted small">{check.name}</span>
                  <span className="dim mono-sm" style={{ marginLeft: 'auto' }}>
                    {check.detail}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
    </>
  )
}

function AgentModal({
  types,
  agent,
  onClose,
  onSaved,
}: {
  types: AgentType[]
  agent: AgentInstance | null
  onClose: () => void
  onSaved: () => void
}) {
  const [typeId, setTypeId] = useState<string | null>(agent?.type_id ?? null)
  const selected = types.find((t) => t.id === typeId) ?? null

  const [name, setName] = useState(agent?.name ?? '')
  const [description, setDescription] = useState(agent?.description ?? '')
  const [gateway, setGateway] = useState(agent?.gateway_url ?? '')
  const [model, setModel] = useState(agent?.model ?? '')
  const [apiKey, setApiKey] = useState('')
  const [options, setOptions] = useState<Record<string, unknown>>(agent?.options ?? {})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const chooseType = (type: AgentType) => {
    setTypeId(type.id)
    if (!agent) {
      const sys = type.system_defaults
      setName((prev) => prev || type.name)
      // 优先读取本机真实配置（如 Claude Code 的 ~/.claude/settings.json），再退回适配器占位值。
      setGateway(sys?.gateway_url ?? type.default_gateway ?? '')
      setModel(sys?.model ?? type.default_model ?? '')
      setOptions(
        Object.fromEntries(type.config_fields.map((f) => [f.key, sys?.options?.[f.key] ?? f.default])),
      )
    }
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
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      wide
      title={agent ? `编辑智能体 · ${agent.name}` : '新建智能体'}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={() => void save()} disabled={saving || !selected}>
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error && <div className="banner err">{error}</div>}

      <div className="field">
        <span className="field-label">适配器类型</span>
        <div className="stack" style={{ gap: 8 }}>
          {types.map((type) => (
            <div
              key={type.id}
              className={[
                'agent-type-row',
                typeId === type.id ? 'selected' : '',
                type.availability.installed ? '' : 'disabled',
              ].join(' ')}
              onClick={() => (agent ? undefined : chooseType(type))}
              style={{ cursor: agent ? 'not-allowed' : 'pointer' }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="agent-type-name">
                  {type.name} <span className="dim mono-sm">{type.vendor}</span>
                </div>
                <div className="agent-type-desc">{type.description}</div>
              </div>
              <div className="chips">
                <span className={`pill ${type.runtime === 'api' ? 'brand' : ''}`}>
                  {type.runtime === 'api' ? '进程内 API' : '本地 CLI'}
                </span>
                {type.availability.installed ? (
                  <span className="pill ok">可用</span>
                ) : (
                  <span className="pill err" title={type.availability.detail ?? ''}>
                    未安装
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {selected && (
        <>
          <SystemDefaultsHint type={selected} />
          <div className="form-grid">
            <div className="field">
              <label className="field-label">名称</label>
              <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 Claude Code（公司网关）" />
            </div>
            <div className="field">
              <label className="field-label">模型</label>
              <input
                className="input mono"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder={selected.default_model ?? '例如 sonnet / gpt-5-codex'}
              />
            </div>
            <div className="field span-2">
              <label className="field-label">
                网关地址
                {selected.base_url_env && <span className="mono-sm dim">→ {selected.base_url_env}</span>}
              </label>
              <input
                className="input mono"
                value={gateway}
                onChange={(e) => setGateway(e.target.value)}
                placeholder={selected.default_gateway ?? 'https://your-gateway.example/v1'}
              />
              <span className="field-help">
                可留空以使用官方地址或环境里的默认凭据。填成中转网关即可统一收口所有智能体的流量。
              </span>
            </div>
            <div className="field span-2">
              <label className="field-label">
                API 密钥
                {selected.api_key_env && <span className="mono-sm dim">→ {selected.api_key_env}</span>}
              </label>
              <div className="secret-row">
                <input
                  className="input mono"
                  type="password"
                  autoComplete="new-password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={agent?.api_key_set ? `已保存 ${agent.api_key_preview ?? ''} · 留空则不修改` : 'sk-...'}
                />
              </div>
              <span className="field-help">密钥使用 Fernet 加密存储，接口只返回掩码，绝不会回传明文。</span>
            </div>
            <div className="field span-2">
              <label className="field-label">备注</label>
              <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
            </div>
          </div>

          <div className="stack">
            <div className="card-section-title">适配器设置</div>
            <DynamicForm fields={selected.config_fields} value={options} onChange={setOptions} />
          </div>
        </>
      )}
    </Modal>
  )
}

function SystemDefaultsHint({ type }: { type: AgentType }) {
  const sys = type.system_defaults
  if (!sys || (!sys.gateway_url && !sys.model && !sys.api_key_set)) return null
  const parts = [
    sys.gateway_url ? `网关 ${sys.gateway_url}` : null,
    sys.model ? `模型 ${sys.model}` : null,
    sys.api_key_set ? `凭据 ${sys.api_key_preview ?? '已配置'}` : null,
  ].filter(Boolean)
  return (
    <div className="banner info">
      已读取该智能体的本机默认配置：{parts.join(' · ')}。
      <span className="dim"> 新建实例时会自动带上这些值，也可以在此覆盖。</span>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
