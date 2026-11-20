import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshIcon } from '../components/Icons'
import { AgentAvatar } from '../components/AgentAvatar'
import { api, ApiError } from '../lib/api'
import { dayInputValue, usageRangeQuery, type UsageRangeKind } from '../lib/usage'
import type { UsageOverview, UsageTokens } from '../lib/types'

const RANGES: { kind: UsageRangeKind; label: string }[] = [
  { kind: 'today', label: '今日' },
  { kind: 'week', label: '一周' },
  { kind: 'month', label: '一月' },
  { kind: 'custom', label: '自定义' },
  { kind: 'all', label: '全部' },
]

const RANGE_HINT: Record<UsageRangeKind, string> = {
  today: '今天 00:00 起',
  week: '近 7 天',
  month: '近 30 天',
  custom: '自定义区间',
  all: '全部记录',
}

/** 一份 token 构成的展示数据：用于环形图的扇区与右侧图例。 */
interface CompositionRow {
  key: string
  label: string
  value: number
  color: string
}

/**
 * 用量页：把平台记录下来的 token 消耗与技能调用汇总到一处。
 *
 * token 数据来自每条助手回复里保存的用量计数（各适配器上报的字段名在后端已归一化）；
 * 技能用量是**调用次数**——从消息里的工具调用识别（Claude Code 的 ``Skill`` 工具），
 * 按所选时间范围统计。右上角可切换今日 / 一周 / 一月 / 自定义 / 全部。
 */
export function UsagePage() {
  const navigate = useNavigate()
  const [data, setData] = useState<UsageOverview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [range, setRange] = useState<UsageRangeKind>('week')
  const [customStart, setCustomStart] = useState(() => dayInputValue(new Date(Date.now() - 6 * 86_400_000)))
  const [customEnd, setCustomEnd] = useState(() => dayInputValue(new Date()))

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await api.usageOverview(usageRangeQuery(range, customStart, customEnd)))
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }, [range, customStart, customEnd])

  useEffect(() => {
    void load()
  }, [load])

  const tokens = data?.tokens
  const skills = data?.skills
  const hasData = Boolean(tokens && skills) && (tokens!.messages > 0 || skills!.calls > 0)
  const todayKey = dayInputValue(new Date())

  const composition = useMemo<CompositionRow[]>(() => {
    if (!tokens) return []
    return [
      { key: 'input', label: '输入', value: tokens.input, color: 'var(--brand)' },
      { key: 'output', label: '输出', value: tokens.output, color: 'var(--accent)' },
      { key: 'cache_read', label: '命中缓存', value: tokens.cache_read, color: 'var(--warn)' },
      { key: 'cache_creation', label: '写入缓存', value: tokens.cache_creation, color: 'var(--text-dim)' },
    ]
  }, [tokens])

  const ringBackground = useMemo(() => {
    const total = composition.reduce((sum, row) => sum + row.value, 0)
    if (total <= 0) return 'var(--bg-hover)'
    let cursor = 0
    const stops = composition
      .filter((row) => row.value > 0)
      .map((row) => {
        const from = (cursor / total) * 100
        cursor += row.value
        const to = (cursor / total) * 100
        return `${row.color} ${from}% ${to}%`
      })
    return stops.length > 0 ? `conic-gradient(${stops.join(', ')})` : 'var(--bg-hover)'
  }, [composition])

  const dailyMax = Math.max(1, ...(data?.daily.map((day) => day.total) ?? [1]))
  const agentMax = Math.max(1, ...(data?.by_agent.map((agent) => agent.tokens.total) ?? [1]))
  const skillMax = Math.max(1, ...(skills?.top.map((skill) => skill.calls) ?? [1]))
  const dailyTotal = data?.daily.reduce((sum, day) => sum + day.total, 0) ?? 0
  const dailyPeak = data?.daily.length
    ? data.daily.reduce((best, day) => (day.total > best.total ? day : best))
    : null
  const dailyAvg = data?.daily.length ? dailyTotal / data.daily.length : 0

  return (
    <>
      <header className="page-header usage-page-head">
        <div>
          <h1 className="page-title">用量</h1>
          <p className="page-desc">
            token 消耗与技能调用的总览，按所选时间范围汇总助手回复里记录的用量。
          </p>
        </div>
        <div className="spacer" />
        <div className="tabs">
          {RANGES.map((item) => (
            <button
              key={item.kind}
              className={`tab${range === item.kind ? ' active' : ''}`}
              onClick={() => setRange(item.kind)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <button
          className={`icon-btn usage-refresh${loading ? ' spinning' : ''}`}
          onClick={() => void load()}
          disabled={loading}
          aria-label="刷新用量"
          title="刷新用量"
        >
          <RefreshIcon size={17} />
        </button>
      </header>

      <div className="page-body stack usage-page">
        {range === 'custom' && (
          <div className="usage-range">
            <span className="usage-range-label">自定义区间</span>
            <input
              className="input"
              type="date"
              style={{ maxWidth: 150 }}
              value={customStart}
              max={customEnd}
              onChange={(e) => setCustomStart(e.target.value)}
            />
            <span className="dim">→</span>
            <input
              className="input"
              type="date"
              style={{ maxWidth: 150 }}
              value={customEnd}
              min={customStart}
              onChange={(e) => setCustomEnd(e.target.value)}
            />
            <span className="usage-range-hint">含起止当天</span>
          </div>
        )}

        {error && <div className="banner err">{error}</div>}
        {loading && !data && <UsageSkeleton />}

        {data && !hasData && (
          <div className="empty usage-empty">
            <div className="empty-icon">📊</div>
            <p>所选时间范围内没有用量记录。</p>
            <p className="dim small">换个范围，或者聊几句再回来看看。</p>
          </div>
        )}

        {data && hasData && tokens && skills && (
          <>
            <section className="usage-hero">
              <div className="usage-hero-main">
                <span className="usage-hero-kicker">总 token 消耗</span>
                <div className="usage-hero-value">
                  {fmt(tokens.total)}
                  <span className="usage-hero-unit">tok</span>
                </div>
                <div className="usage-hero-chips">
                  <span className="usage-chip">统计消息 {fmt(tokens.messages)} 条</span>
                  <span className="usage-chip">{fmt(data.by_agent.length)} 个智能体</span>
                  <span className="usage-chip">{fmt(skills.calls)} 次技能调用</span>
                </div>
                <p className="usage-hero-note">
                  {RANGE_HINT[range]} · 数据来自助手回复上报的用量
                </p>
              </div>

              <div className="usage-hero-side">
                <div
                  className="usage-ring"
                  style={{ background: ringBackground }}
                  role="img"
                  aria-label="token 构成环形图"
                >
                  <div className="usage-ring-inner">
                    <strong>{cacheRate(tokens)}</strong>
                    <span>缓存命中率</span>
                  </div>
                </div>
                <ul className="usage-legend">
                  {composition.map((row) => (
                    <li key={row.key}>
                      <span className="usage-dot" style={{ background: row.color }} />
                      <span className="usage-legend-label">{row.label}</span>
                      <span className="usage-legend-value">{compact(row.value)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </section>

            {data.daily.length > 0 && (
              <section className="usage-panel">
                <div className="usage-panel-head">
                  <div>
                    <h2 className="usage-panel-title">每日消耗</h2>
                    <p className="usage-panel-sub">按本地自然日汇总，柱高代表当天总 token</p>
                  </div>
                  <ul className="usage-legend inline">
                    <li>
                      <span className="usage-dot" style={{ background: 'var(--brand)' }} />
                      <span className="usage-legend-label">输入</span>
                    </li>
                    <li>
                      <span className="usage-dot" style={{ background: 'var(--accent)' }} />
                      <span className="usage-legend-label">输出</span>
                    </li>
                    <li>
                      <span
                        className="usage-dot"
                        style={{ background: 'linear-gradient(90deg, var(--brand), var(--accent))', opacity: 0.45 }}
                      />
                      <span className="usage-legend-label">缓存</span>
                    </li>
                  </ul>
                </div>

                <div className="usage-chart-wrap">
                  <div className="usage-chart" role="img" aria-label="每日 token 消耗柱状图">
                    <div
                      className={`usage-chart-cols${data.daily.length <= 7 ? ' sparse' : ''}`}
                    >
                      {data.daily.map((day) => {
                        const cache = Math.max(0, day.total - day.input - day.output)
                        return (
                          <div
                            className={`usage-chart-col${day.date === todayKey ? ' today' : ''}`}
                            key={day.date}
                            title={`${day.date} · 输入 ${fmt(day.input)} · 输出 ${fmt(day.output)} · 缓存 ${fmt(cache)} · 共 ${fmt(day.total)}`}
                          >
                            <div className="usage-chart-track">
                              <span
                                className="usage-chart-seg seg-input"
                                style={{ height: barHeight(day.input, dailyMax) }}
                              />
                              <span
                                className="usage-chart-seg seg-output"
                                style={{ height: barHeight(day.output, dailyMax) }}
                              />
                              <span
                                className="usage-chart-seg seg-cache"
                                style={{ height: barHeight(cache, dailyMax) }}
                              />
                            </div>
                            <span className="usage-chart-date">{day.date.slice(5)}</span>
                          </div>
                        )
                      })}
                    </div>
                  </div>

                  {dailyPeak && (
                    <aside className="usage-chart-aside">
                      <div className="usage-chart-stat">
                        <span>峰值</span>
                        <strong>{compact(dailyPeak.total)}</strong>
                        <em>{dailyPeak.date.slice(5)}</em>
                      </div>
                      <div className="usage-chart-stat">
                        <span>日均</span>
                        <strong>{compact(dailyAvg)}</strong>
                        <em>{data.daily.length} 个活跃日</em>
                      </div>
                      <div className="usage-chart-stat">
                        <span>区间合计</span>
                        <strong>{compact(dailyTotal)}</strong>
                        <em>tok</em>
                      </div>
                    </aside>
                  )}
                </div>
              </section>
            )}

            <div className="usage-columns">
              {data.by_agent.length > 0 && (
                <section className="usage-panel">
                  <div className="usage-panel-head">
                    <div>
                      <h2 className="usage-panel-title">按智能体</h2>
                      <p className="usage-panel-sub">哪个智能体在替你消耗 token</p>
                    </div>
                  </div>

                  <div className="usage-agent-list">
                    {data.by_agent.map((agent, index) => (
                      <div className="usage-agent" key={agent.id}>
                        <span className="usage-rank">{rank(index)}</span>
                        <AgentAvatar avatar={agent.avatar} avatarPreset={agent.avatar_preset} size={26} className="usage-agent-avatar" />
                        <div className="usage-agent-main">
                          <div className="usage-agent-line">
                            <span className="usage-agent-name">{agent.name}</span>
                            <span className="usage-agent-total">
                              {fmt(agent.tokens.total)}
                              <em>tok</em>
                            </span>
                          </div>
                          <div className="usage-agent-meta">
                            {agent.type_name ?? agent.type_id ?? '未知内核'} · 输入 {compact(agent.tokens.input)} ·
                            输出 {compact(agent.tokens.output)}
                          </div>
                          <div className="usage-agent-track">
                            <span
                              style={{ width: `${Math.max(2, (agent.tokens.total / agentMax) * 100)}%` }}
                            />
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <section className="usage-panel">
                <div className="usage-panel-head">
                  <div>
                    <h2 className="usage-panel-title">技能用量</h2>
                    <p className="usage-panel-sub">按调用次数排行</p>
                  </div>
                </div>

                <div className="usage-skill-summary">
                  <div>
                    <strong>{fmt(skills.calls)}</strong>
                    <span>调用总次数</span>
                  </div>
                  <div>
                    <strong>{fmt(skills.skills_used)}</strong>
                    <span>涉及技能</span>
                  </div>
                </div>

                {skills.top.length > 0 ? (
                  <div className="usage-skill-list">
                    {skills.top.map((skill, index) => (
                      <button
                        className="usage-skill"
                        key={skill.id}
                        onClick={() => navigate('/skills')}
                        title="到技能页查看"
                      >
                        <span className="usage-rank">{rank(index)}</span>
                        <span className="usage-skill-main">
                          <span className="usage-skill-line">
                            <span className="usage-skill-name">{skill.name}</span>
                            <span className="usage-skill-calls">
                              {fmt(skill.calls)}
                              <em>次</em>
                            </span>
                          </span>
                          <span className="usage-skill-meta">
                            {skill.type_name ?? skill.type_id}
                            {skill.source_label ? ` · ${skill.source_label}` : ''}
                          </span>
                          <span className="usage-skill-track">
                            <span style={{ width: `${Math.max(2, (skill.calls / skillMax) * 100)}%` }} />
                          </span>
                        </span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="usage-empty-line">所选时间范围内没有技能调用。</p>
                )}

                <p className="usage-note">
                  技能调用按消息里的工具调用识别（如 Claude Code 的 Skill 工具），只统计所选时间范围内发生的调用；
                  未声明调用形式的内核不会被计入。
                </p>
              </section>
            </div>
          </>
        )}
      </div>
    </>
  )
}

/** 首屏骨架：只在还没有数据时出现，刷新时保留旧数据，避免页面闪一下。 */
function UsageSkeleton() {
  return (
    <div className="usage-skeleton" aria-hidden="true">
      <div className="usage-skeleton-hero" />
      <div className="usage-skeleton-panel" />
      <div className="usage-skeleton-grid">
        <div className="usage-skeleton-panel" />
        <div className="usage-skeleton-panel" />
      </div>
    </div>
  )
}

function fmt(value: number): string {
  return value.toLocaleString('en-US')
}

/** 图例 / 图表轴上的紧凑数字：1234567 → 1.2M。 */
function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

function cacheRate(tokens: UsageTokens): string {
  return tokens.cache_ratio === null ? '—' : `${Math.round(tokens.cache_ratio * 100)}%`
}

function barHeight(value: number, max: number): string {
  if (value <= 0) return '0'
  return `${Math.max(1.2, (value / max) * 100)}%`
}

function rank(index: number): string {
  return String(index + 1).padStart(2, '0')
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
