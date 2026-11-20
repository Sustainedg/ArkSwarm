import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshIcon } from '../../components/Icons'
import { AgentAvatar } from '../../components/AgentAvatar'
import { api } from '../../lib/api'
import { dayInputValue, usageRangeQuery, type UsageRangeKind } from '../../lib/usage'
import type { UsageOverview, UsageTokens } from '../../lib/types'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const RANGES: { kind: UsageRangeKind; label: string }[] = [
  { kind: 'today', label: '今日' },
  { kind: 'week', label: '一周' },
  { kind: 'month', label: '一月' },
  { kind: 'custom', label: '自定义' },
  { kind: 'all', label: '全部' },
]

const RANGE_HINT: Record<UsageRangeKind, string> = {
  today: '今天',
  week: '近 7 天',
  month: '近 30 天',
  custom: '自定义',
  all: '全部',
}

interface CompositionRow {
  key: string
  label: string
  value: number
  color: string
}

/**
 * 用量：token 消耗与技能调用总览。
 *
 * 与桌面端 `pages/UsagePage` 功能对齐（数据同源、口径一致），按触屏重排成单栏卡片；
 * 移动端不引用桌面端的页面与样式表，这是两份实现；范围换算共用 `lib/usage.ts`。
 */
export function MobileUsagePage() {
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
      setError(describeError(e))
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

  return (
    <div className="m-page">
      <MobileTopBar
        title="用量"
        subtitle="token 消耗与技能调用"
        actions={
          <button
            className="m-icon-btn"
            onClick={() => void load()}
            aria-label="刷新用量"
            title="刷新用量"
            disabled={loading}
          >
            <RefreshIcon size={17} className={loading ? 'm-spin' : undefined} />
          </button>
        }
      />

      <div className="m-page-body stack">
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

        {range === 'custom' && (
          <div className="m-usage-range-row">
            <input
              className="input"
              type="date"
              value={customStart}
              max={customEnd}
              onChange={(e) => setCustomStart(e.target.value)}
            />
            <span className="dim">→</span>
            <input
              className="input"
              type="date"
              value={customEnd}
              min={customStart}
              onChange={(e) => setCustomEnd(e.target.value)}
            />
          </div>
        )}

        {error && <div className="banner err">{error}</div>}
        {loading && !data && <p className="dim small">正在汇总…</p>}
        {data && !hasData && (
          <div className="m-empty">
            <div className="m-empty-icon">📊</div>
            <p>所选时间范围内没有用量记录</p>
            <p className="dim small">换个范围，或者聊几句再回来看看。</p>
          </div>
        )}

        {data && hasData && tokens && skills && (
          <>
            <section className="m-usage-hero">
              <div className="m-usage-hero-head">
                <span className="m-usage-kicker">总 token</span>
                <span className="m-usage-scope">{RANGE_HINT[range]}</span>
              </div>
              <div className="m-usage-hero-value">
                {fmt(tokens.total)}
                <span>tok</span>
              </div>
              <div className="m-usage-chips">
                <span>{fmt(tokens.messages)} 条消息</span>
                <span>{fmt(data.by_agent.length)} 个智能体</span>
                <span>{fmt(skills.calls)} 次技能调用</span>
              </div>

              <div className="m-usage-ring-row">
                <div
                  className="m-usage-ring"
                  style={{ background: ringBackground }}
                  role="img"
                  aria-label="token 构成环形图"
                >
                  <div className="m-usage-ring-inner">
                    <strong>{cacheRate(tokens)}</strong>
                    <span>缓存命中</span>
                  </div>
                </div>
                <ul className="m-usage-legend">
                  {composition.map((row) => (
                    <li key={row.key}>
                      <span className="m-usage-dot" style={{ background: row.color }} />
                      <span className="m-usage-legend-label">{row.label}</span>
                      <span className="m-usage-legend-value">{compact(row.value)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </section>

            {data.daily.length > 0 && (
              <section className="m-usage-panel">
                <div className="m-usage-panel-head">
                  <span className="m-usage-panel-title">每日消耗</span>
                  <span className="m-usage-panel-sub">柱高 = 当天总 token</span>
                </div>
                <div className="m-usage-chart">
                  {data.daily.map((day) => {
                    const cache = Math.max(0, day.total - day.input - day.output)
                    return (
                      <div className={`m-usage-chart-row${day.date === todayKey ? ' today' : ''}`} key={day.date}>
                        <span className="m-usage-chart-date">{day.date.slice(5)}</span>
                        <span className="m-usage-chart-track">
                          <span
                            className="m-usage-chart-seg seg-input"
                            style={{ width: segWidth(day.input, dailyMax) }}
                          />
                          <span
                            className="m-usage-chart-seg seg-output"
                            style={{ width: segWidth(day.output, dailyMax) }}
                          />
                          <span
                            className="m-usage-chart-seg seg-cache"
                            style={{ width: segWidth(cache, dailyMax) }}
                          />
                        </span>
                        <span className="m-usage-chart-value">{compact(day.total)}</span>
                      </div>
                    )
                  })}
                </div>
                <div className="m-usage-chart-legend">
                  <span>
                    <i style={{ background: 'var(--brand)' }} />
                    输入
                  </span>
                  <span>
                    <i style={{ background: 'var(--accent)' }} />
                    输出
                  </span>
                  <span>
                    <i style={{ background: 'linear-gradient(90deg, var(--brand), var(--accent))', opacity: 0.45 }} />
                    缓存
                  </span>
                </div>
              </section>
            )}

            {data.by_agent.length > 0 && (
              <section className="m-usage-panel">
                <div className="m-usage-panel-head">
                  <span className="m-usage-panel-title">按智能体</span>
                  <span className="m-usage-panel-sub">token 去向</span>
                </div>
                <div className="m-usage-agent-list">
                  {data.by_agent.map((agent, index) => (
                    <div className="m-usage-agent" key={agent.id}>
                      <span className="m-usage-rank">{rank(index)}</span>
                      <AgentAvatar avatar={agent.avatar} avatarPreset={agent.avatar_preset} size={24} className="m-usage-avatar" />
                      <span className="m-usage-agent-main">
                        <span className="m-usage-agent-line">
                          <span className="m-usage-agent-name">{agent.name}</span>
                          <span className="m-usage-agent-total">
                            {fmt(agent.tokens.total)}
                            <em>tok</em>
                          </span>
                        </span>
                        <span className="m-usage-agent-meta">
                          {agent.type_name ?? agent.type_id ?? '未知内核'} · 输入 {compact(agent.tokens.input)} · 输出{' '}
                          {compact(agent.tokens.output)}
                        </span>
                        <span className="m-usage-agent-track">
                          <span style={{ width: `${Math.max(2, (agent.tokens.total / agentMax) * 100)}%` }} />
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            <section className="m-usage-panel">
              <div className="m-usage-panel-head">
                <span className="m-usage-panel-title">技能用量</span>
                <span className="m-usage-panel-sub">按调用次数</span>
              </div>

              <div className="m-usage-skill-summary">
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
                <div className="m-usage-skill-list">
                  {skills.top.map((skill, index) => (
                    <button
                      className="m-usage-skill"
                      key={skill.id}
                      onClick={() => navigate('/skills')}
                      title="到技能页查看"
                    >
                      <span className="m-usage-rank">{rank(index)}</span>
                      <span className="m-usage-skill-main">
                        <span className="m-usage-skill-line">
                          <span className="m-usage-skill-name">{skill.name}</span>
                          <span className="m-usage-skill-calls">
                            {fmt(skill.calls)}
                            <em>次</em>
                          </span>
                        </span>
                        <span className="m-usage-skill-meta">
                          {skill.type_name ?? skill.type_id}
                          {skill.source_label ? ` · ${skill.source_label}` : ''}
                        </span>
                        <span className="m-usage-skill-track">
                          <span style={{ width: `${Math.max(2, (skill.calls / skillMax) * 100)}%` }} />
                        </span>
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="m-usage-empty-line">所选时间范围内没有技能调用。</p>
              )}

              <p className="m-usage-note">
                技能调用按消息里的工具调用识别（如 Claude Code 的 Skill 工具），只统计所选时间范围内发生的调用；
                未声明调用形式的内核不会被计入。
              </p>
            </section>
          </>
        )}
      </div>
    </div>
  )
}

function fmt(value: number): string {
  return value.toLocaleString('en-US')
}

function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

function cacheRate(tokens: UsageTokens): string {
  return tokens.cache_ratio === null ? '—' : `${Math.round(tokens.cache_ratio * 100)}%`
}

function segWidth(value: number, max: number): string {
  if (value <= 0) return '0'
  return `${Math.max(1.2, (value / max) * 100)}%`
}

function rank(index: number): string {
  return String(index + 1).padStart(2, '0')
}
