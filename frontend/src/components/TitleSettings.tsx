import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { formatListTime } from '../lib/datetime'
import type { AgentProfile, Gateway, TitleSettings } from '../lib/types'

/**
 * 「设置 → 对话标题」：新对话的标题用哪个智能体、哪个模型自动生成。
 *
 * 桌面端与移动端共用这一份（数据走 `api.getTitleSettings` / `setTitleSettings`）：
 * 不指定智能体就跟随「默认智能体」，模型留空就用那个智能体的默认模型 ——
 * 也就是「默认智能体的默认模型」这条默认姿势。
 *
 * 生成是尽力而为的：跑不出来时保留首行的兜底标题，所以这个开关坏了也不会影响对话。
 */
export function TitleSettingsSection() {
  const [settings, setSettings] = useState<TitleSettings | null>(null)
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [gateways, setGateways] = useState<Gateway[]>([])
  const [enabled, setEnabled] = useState(true)
  const [agentId, setAgentId] = useState('')
  const [model, setModel] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const reload = async () => {
    try {
      const [title, agentList, gatewayList] = await Promise.all([
        api.getTitleSettings(),
        api.listAgents(),
        api.listGateways(),
      ])
      setSettings(title)
      setAgents(agentList)
      setGateways(gatewayList.items)
      setEnabled(title.enabled)
      setAgentId(title.agent_profile_id ?? '')
      setModel(title.model ?? '')
      setError(null)
    } catch (e) {
      setError(describe(e))
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  // 实际生效的智能体：选了就用选的，没选就用「默认智能体」（后端已经解析好 id 与名字）。
  const effectiveId = agentId || settings?.default_agent_id || ''
  const effective = agents.find((agent) => agent.id === effectiveId) ?? null
  const gateway = gateways.find((item) => item.id === effective?.gateway_id) ?? null
  const modelChoices = gateway?.selected_models ?? []
  const dirty =
    settings !== null &&
    (enabled !== settings.enabled ||
      (agentId || null) !== (settings.agent_profile_id ?? null) ||
      (model.trim() || null) !== (settings.model ?? null))

  const save = async () => {
    setSaving(true)
    setError(null)
    setSaved(false)
    try {
      const next = await api.setTitleSettings({
        enabled,
        agent_profile_id: agentId || null,
        model: model.trim() || null,
      })
      setSettings(next)
      setEnabled(next.enabled)
      setAgentId(next.agent_profile_id ?? '')
      setModel(next.model ?? '')
      setSaved(true)
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="stack">
      <h2 className="card-section-title">对话标题</h2>
      <div className="card">
        <p className="dim small">
          新对话在第一句话之后会自动起一个简短标题，替代默认的首行摘要。不指定智能体就跟随
          「默认智能体」，模型留空就用该智能体的默认模型。起标题失败时会保留首行标题，不影响对话。
        </p>
        {error && <div className="banner err">{error}</div>}
        {saved && <div className="banner ok">已保存</div>}
        {/* 起标题是后台增强：失败只在日志里用户看不到，这里给一个自查出口（成功一次就自动消失）。 */}
        {settings?.last_error && (
          <div className="banner warn">
            上次自动起标题失败：{settings.last_error}
            {settings.last_error_at ? `（${formatListTime(settings.last_error_at)}）` : ''}
            。对话会保留首行的兜底标题，不影响使用；换一个智能体 / 模型通常就能好。
          </div>
        )}

        <label className="switch">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          <span>自动生成对话标题</span>
        </label>

        <div className="row wrap" style={{ gap: 10, alignItems: 'flex-end' }}>
          <div style={{ flex: 1, minWidth: 180 }}>
            <label className="field-label">用哪个智能体</label>
            <select
              className="select"
              value={agentId}
              onChange={(event) => {
                setAgentId(event.target.value)
                setModel('')
                setSaved(false)
              }}
              disabled={!enabled}
            >
              <option value="">
                跟随默认智能体
                {settings?.default_agent_name ? `（${settings.default_agent_name}）` : '（暂无）'}
              </option>
              {agents.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name} · {agent.type_name ?? agent.type_id}
                </option>
              ))}
            </select>
          </div>

          <div style={{ flex: 1, minWidth: 180 }}>
            <label className="field-label">用哪个模型</label>
            {gateway && modelChoices.length > 0 ? (
              <select
                className="select"
                value={model}
                onChange={(event) => {
                  setModel(event.target.value)
                  setSaved(false)
                }}
                disabled={!enabled}
              >
                <option value="">默认（{effective?.model ?? effective?.type_name ?? '智能体的默认模型'}）</option>
                {modelChoices.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            ) : (
              <input
                className="input mono"
                value={model}
                onChange={(event) => {
                  setModel(event.target.value)
                  setSaved(false)
                }}
                placeholder="留空 = 智能体的默认模型"
                disabled={!enabled}
              />
            )}
          </div>

          <button className="btn primary" onClick={() => void save()} disabled={saving || !dirty}>
            {saving ? '保存中…' : '保存'}
          </button>
        </div>

        <p className="dim small">
          {settings?.effective_agent_name
            ? `当前生效：${settings.effective_agent_name}`
            : '还没有可用的智能体：先到「智能体」页创建，并点卡片右上角的五角星设为默认。'}
        </p>
      </div>
    </section>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
