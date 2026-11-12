import { useCallback, useEffect, useState } from 'react'
import { api } from '../../lib/api'
import type { Gateway, GatewayList } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/**
 * 自定义网关（移动端）：网关是 API 密钥的唯一来源。
 *
 * 这里只列出用户自建的自定义网关：创建时必须「获取模型」成功（或在手动旁路里录入）
 * 并至少勾选一个模型 —— 服务端也把它作为保存的不变量。
 * 内核的官方链路不在这里出现 —— 智能体配置里选「官方默认」就等于平台不接管路由。
 */
export function MobileGatewaysPage() {
  const [data, setData] = useState<GatewayList | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Gateway | null>(null)

  const reload = useCallback(async () => {
    try {
      setData(await api.listGateways())
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const remove = async (gateway: Gateway) => {
    if (!confirm(`删除网关「${gateway.name}」？`)) return
    try {
      await api.deleteGateway(gateway.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const gateways = (data?.items ?? []).filter((item) => item.kind === 'custom')

  return (
    <div className="m-page">
      <MobileTopBar
        title="自定义网关"
        subtitle="API 密钥的唯一来源"
        actions={
          <button className="m-icon-btn" onClick={() => setCreating(true)} aria-label="新建网关">
            ＋
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}
          <div className="banner info">
            自定义网关必须先「获取模型」成功并至少勾选一个模型才能保存。内核的官方链路不在这里维护 ——
            在智能体配置里选「官方默认」即可。
          </div>

          {loading ? (
            <p className="dim small">正在加载网关…</p>
          ) : gateways.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">🔌</div>
              <p>还没有自定义网关</p>
            </div>
          ) : (
            <div className="m-list">
              {gateways.map((gateway) => (
                <div className="card" key={gateway.id}>
                  <div className="row">
                    <div className="m-item-main">
                      <div className="m-item-title">{gateway.name}</div>
                      <div className="m-item-sub mono-sm">{gateway.base_url}</div>
                    </div>
                    <span className="pill">自定义</span>
                  </div>
                  <div className="row wrap" style={{ gap: 6 }}>
                    <span className="pill">{gateway.selected_models.length} 个模型</span>
                    {gateway.api_key_set ? (
                      <span className="pill">🔑 {gateway.api_key_preview}</span>
                    ) : (
                      <span className="pill warn">未配置密钥</span>
                    )}
                    {gateway.used_by_profiles > 0 && <span className="pill ok">{gateway.used_by_profiles} 个智能体在用</span>}
                  </div>
                  <div className="m-item-actions" style={{ marginTop: 0 }}>
                    <button className="btn sm" onClick={() => setEditing(gateway)}>
                      编辑
                    </button>
                    <button className="btn sm danger" onClick={() => void remove(gateway)}>
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {(creating || editing) && data && (
        <MobileSheet
          full
          title={editing ? `编辑网关 · ${editing.name}` : '新建网关'}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
        >
          <GatewayForm
            gateway={editing}
            protocols={data.protocols}
            authStyles={data.auth_styles}
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

function GatewayForm({
  gateway,
  protocols,
  authStyles,
  onSaved,
}: {
  gateway: Gateway | null
  protocols: { value: string; label: string }[]
  authStyles: { value: string; label: string }[]
  onSaved: () => void
}) {
  const [name, setName] = useState(gateway?.name ?? '')
  const [baseUrl, setBaseUrl] = useState(gateway?.base_url ?? '')
  const [protocol, setProtocol] = useState(gateway?.protocol ?? protocols[0]?.value ?? 'openai')
  const [authStyle, setAuthStyle] = useState(gateway?.auth_style ?? authStyles[0]?.value ?? 'bearer')
  const [apiKey, setApiKey] = useState('')
  const [catalog, setCatalog] = useState<string[]>(gateway?.model_catalog ?? gateway?.selected_models ?? [])
  const [selected, setSelected] = useState<string[]>(gateway?.selected_models ?? [])
  const [manual, setManual] = useState('')
  const [probing, setProbing] = useState(false)
  const [probeMessage, setProbeMessage] = useState<string | null>(null)
  const [bypass, setBypass] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const probe = async () => {
    if (!baseUrl.trim()) return setError('请先填写 API 地址')
    setProbing(true)
    setError(null)
    setProbeMessage(null)
    try {
      const result = await api.probeGateway({
        base_url: baseUrl.trim(),
        protocol,
        auth_style: authStyle,
        api_key: apiKey.trim() || null,
        gateway_id: gateway?.id ?? null,
      })
      setProbeMessage(result.message)
      if (result.ok) {
        setCatalog(result.models)
        setSelected(result.models)
        setBypass(false)
      } else {
        setBypass(true)
      }
    } catch (e) {
      setProbeMessage(describeError(e))
      setBypass(true)
    } finally {
      setProbing(false)
    }
  }

  const toggle = (model: string) =>
    setSelected((prev) => (prev.includes(model) ? prev.filter((item) => item !== model) : [...prev, model]))

  const addManual = () => {
    const items = manual
      .split(/[\n,，、]/)
      .map((item) => item.trim())
      .filter(Boolean)
    if (items.length === 0) return
    setCatalog((prev) => Array.from(new Set([...prev, ...items])))
    setSelected((prev) => Array.from(new Set([...prev, ...items])))
    setManual('')
  }

  const save = async () => {
    if (!name.trim()) return setError('请填写网关名称')
    if (!baseUrl.trim()) return setError('请填写 API 地址')
    if (selected.length === 0) return setError('请先「获取模型」并至少勾选一个模型（或手动录入）')
    setSaving(true)
    setError(null)
    try {
      const payload: Record<string, unknown> = {
        name: name.trim(),
        base_url: baseUrl.trim(),
        protocol,
        auth_style: authStyle,
        selected_models: selected,
      }
      if (apiKey.trim()) payload.api_key = apiKey.trim()
      if (gateway) await api.updateGateway(gateway.id, payload)
      else await api.createGateway(payload)
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
        <label className="field-label">名称</label>
        <input className="input" value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 公司中转网关" />
      </div>

      <div className="field">
        <label className="field-label">API 地址</label>
        <input
          className="input mono"
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
          placeholder="https://your-gateway.example/v1"
        />
      </div>

      <div className="field">
        <label className="field-label">协议</label>
        <select className="select" value={protocol} onChange={(event) => setProtocol(event.target.value)}>
          {protocols.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="field-label">鉴权方式</label>
        <select className="select" value={authStyle} onChange={(event) => setAuthStyle(event.target.value)}>
          {authStyles.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label className="field-label">API 密钥</label>
        <input
          className="input mono"
          type="password"
          autoComplete="new-password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder={gateway?.api_key_set ? `已保存 ${gateway.api_key_preview ?? ''} · 留空则不修改` : 'sk-...'}
        />
        <span className="field-help">密钥经 Fernet 加密存储，接口只返回掩码，绝不回传明文。</span>
      </div>

      <div className="stack" style={{ gap: 8 }}>
        <div className="row" style={{ gap: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>
            模型
          </span>
          <div className="spacer" style={{ flex: 1 }} />
          <button className="btn sm" onClick={() => void probe()} disabled={probing}>
            {probing ? '获取中…' : '⬇ 获取模型'}
          </button>
          <button className="btn sm ghost" onClick={() => setBypass((prev) => !prev)}>
            {bypass ? '收起旁路' : '手动旁路'}
          </button>
        </div>

        {probeMessage && <div className={`banner ${selected.length > 0 ? 'info' : 'warn'}`}>{probeMessage}</div>}

        {bypass && (
          <div className="field">
            <label className="field-label">手动录入模型名</label>
            <div className="row" style={{ gap: 8 }}>
              <input
                className="input mono"
                value={manual}
                onChange={(event) => setManual(event.target.value)}
                placeholder="逗号 / 换行分隔"
              />
              <button className="btn sm" onClick={addManual}>
                添加
              </button>
            </div>
          </div>
        )}

        {catalog.length > 0 ? (
          <>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn sm ghost" onClick={() => setSelected(catalog)}>
                全选
              </button>
              <button className="btn sm ghost" onClick={() => setSelected([])}>
                清空
              </button>
              <span className="dim small">已勾选 {selected.length} 个</span>
            </div>
            <div className="stack" style={{ gap: 8 }}>
              {catalog.map((model) => (
                <label key={model} className="skill-option">
                  <input type="checkbox" checked={selected.includes(model)} onChange={() => toggle(model)} />
                  <span className="skill-option-body">
                    <span className="skill-option-name mono-sm">{model}</span>
                  </span>
                </label>
              ))}
            </div>
          </>
        ) : (
          <p className="dim small">点「获取模型」拉取清单；拉不到就用「手动旁路」录入。</p>
        )}
      </div>

      <button className="btn primary block" onClick={() => void save()} disabled={saving}>
        {saving ? '保存中…' : '保存网关'}
      </button>
    </>
  )
}
