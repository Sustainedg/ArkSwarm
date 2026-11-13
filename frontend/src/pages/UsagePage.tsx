import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { formatListTime } from '../lib/datetime'
import { dayInputValue, usageRangeQuery, type UsageRangeKind } from '../lib/usage'
import type { UsageOverview } from '../lib/types'

const RANGES: { kind: UsageRangeKind; label: string }[] = [
  { kind: 'today', label: '今日' },
  { kind: 'week', label: '一周' },
  { kind: 'month', label: '一月' },
  { kind: 'custom', label: '自定义' },
  { kind: 'all', label: '全部' },
]

/**
 * 用量页：把平台记录下来的 token 消耗与技能调用汇总到一处。
 *
 * token 数据来自每条助手回复里保存的用量计数（各适配器上报的字段名在后端已归一化，
 * 与对话页「本对话消耗」是同一套口径）；技能用量是**调用次数**——从消息里的工具调用识别
 * （Claude Code 的 ``Skill`` 工具），按所选时间范围统计。右上角可切换今日 / 一周 / 一月 /
 * 自定义 / 全部。
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
  const dailyMax = Math.max(1, ...(data?.daily.map((day) => day.total) ?? [1]))

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">用量</h1>
          <p className="page-desc">
            token 消耗与技能调用的总览：助手回复里记录的用量在这里按日期、智能体与对话汇总。
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
        {range === 'custom' && (
          <div className="row" style={{ gap: 6 }}>
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
          </div>
        )}
        <button className="btn" onClick={() => void load()} disabled={loading}>
          ↻ 刷新
        </button>
      </header>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}
        {loading && !data && <p className="dim small">正在汇总…</p>}

        {data && !hasData && (
          <div className="empty">
            <div className="empty-icon">📊</div>
            <p>所选时间范围内没有用量记录。换个范围，或者聊几句再回来看看。</p>
          </div>
        )}

        {data && hasData && tokens && skills && (
          <>
            <section className="stack">
              <h2 className="card-section-title">token 消耗</h2>
              <div className="usage-stats">
                <Stat label="总 token" value={fmt(tokens.total)} unit="tok" highlight />
                <Stat label="输入" value={fmt(tokens.input)} unit="tok" />
                <Stat label="输出" value={fmt(tokens.output)} unit="tok" />
                <Stat
                  label="缓存命中率"
                  value={tokens.cache_ratio === null ? '—' : `${Math.round(tokens.cache_ratio * 100)}%`}
                />
                <Stat label="命中缓存" value={fmt(tokens.cache_read)} unit="tok" />
                <Stat label="写入缓存" value={fmt(tokens.cache_creation)} unit="tok" />
                <Stat label="统计消息" value={fmt(tokens.messages)} unit="条" />
                <Stat label="涉及对话" value={fmt(data.conversations)} unit="条" />
              </div>
            </section>

            {data.daily.length > 0 && (
              <section className="stack">
                <h2 className="card-section-title">每日消耗</h2>
                <div className="usage-bars">
                  {data.daily.map((day) => (
                    <div className="usage-bar-row" key={day.date}>
                      <span className="dim small">{day.date.slice(5)}</span>
                      <span className="usage-bar-track">
                        <span
                          className="usage-bar-fill"
                          style={{ width: `${Math.max(2, (day.total / dailyMax) * 100)}%` }}
                        />
                      </span>
                      <span className="usage-bar-value" title={`输入 ${fmt(day.input)} · 输出 ${fmt(day.output)}`}>
                        {compact(day.total)}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {data.by_agent.length > 0 && (
              <section className="stack">
                <h2 className="card-section-title">按智能体</h2>
                <div className="usage-rows">
                  {data.by_agent.map((agent) => (
                    <div className="usage-row" key={agent.id}>
                      <div className="usage-row-main">
                        <div className="usage-row-title">
                          {agent.avatar} {agent.name}
                        </div>
                        <div className="usage-row-meta">
                          {agent.type_name ?? agent.type_id ?? '未知内核'} · 输入 {fmt(agent.tokens.input)} · 输出{' '}
                          {fmt(agent.tokens.output)}
                        </div>
                      </div>
                      <div className="usage-row-num">
                        <strong>{fmt(agent.tokens.total)}</strong> <span className="dim small">tok</span>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {data.by_conversation.length > 0 && (
              <section className="stack">
                <h2 className="card-section-title">按对话（消耗最多的 {data.by_conversation.length} 条）</h2>
                <div className="usage-rows">
                  {data.by_conversation.map((row) => (
                    <button
                      className="usage-row clickable"
                      key={row.id}
                      onClick={() => navigate(`/chat/${row.id}`)}
                      title="打开这条对话"
                    >
                      <div className="usage-row-main">
                        <div className="usage-row-title">{row.title}</div>
                        <div className="usage-row-meta">
                          {row.agent_name ?? '—'}
                          {row.updated_at ? ` · ${formatListTime(row.updated_at)}` : ''}
                        </div>
                      </div>
                      <div className="usage-row-num">
                        <strong>{fmt(row.tokens.total)}</strong> <span className="dim small">tok</span>
                      </div>
                    </button>
                  ))}
                </div>
              </section>
            )}

            <section className="stack">
              <h2 className="card-section-title">技能用量</h2>
              <div className="usage-stats">
                <Stat label="调用总次数" value={fmt(skills.calls)} unit="次" highlight />
                <Stat label="涉及技能" value={fmt(skills.skills_used)} unit="个" />
              </div>
              <p className="dim small">
                技能调用按消息里的工具调用识别（如 Claude Code 的 Skill 工具），只统计所选时间范围内发生的调用；
                未声明调用形式的内核不会被计入。
              </p>

              {skills.top.length > 0 ? (
                <div className="usage-rows">
                  {skills.top.map((skill) => (
                    <button
                      className="usage-row clickable"
                      key={skill.id}
                      onClick={() => navigate('/skills')}
                      title="到技能页查看"
                    >
                      <div className="usage-row-main">
                        <div className="usage-row-title">{skill.name}</div>
                        <div className="usage-row-meta">
                          {skill.type_name ?? skill.type_id}
                          {skill.source_label ? ` · ${skill.source_label}` : ''}
                        </div>
                      </div>
                      <div className="usage-row-num">
                        <strong>{fmt(skill.calls)}</strong> <span className="dim small">次</span>
                      </div>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="dim small">所选时间范围内没有技能调用。</p>
              )}
            </section>
          </>
        )}
      </div>
    </>
  )
}

function Stat({
  label,
  value,
  unit,
  highlight,
}: {
  label: string
  value: string
  unit?: string
  highlight?: boolean
}) {
  return (
    <div className={`usage-stat${highlight ? ' highlight' : ''}`}>
      <span className="usage-stat-label">{label}</span>
      <span className="usage-stat-value">
        {value}
        {unit && <span className="usage-stat-unit">{unit}</span>}
      </span>
    </div>
  )
}

function fmt(value: number): string {
  return value.toLocaleString('en-US')
}

/** 图表轴上的紧凑数字：1234567 → 1.2M。 */
function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
