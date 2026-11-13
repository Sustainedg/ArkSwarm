import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatListTime } from '../../lib/datetime'
import { dayInputValue, usageRangeQuery, type UsageRangeKind } from '../../lib/usage'
import type { UsageOverview } from '../../lib/types'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const RANGES: { kind: UsageRangeKind; label: string }[] = [
  { kind: 'today', label: '今日' },
  { kind: 'week', label: '一周' },
  { kind: 'month', label: '一月' },
  { kind: 'custom', label: '自定义' },
  { kind: 'all', label: '全部' },
]

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
  const dailyMax = Math.max(1, ...(data?.daily.map((day) => day.total) ?? [1]))

  return (
    <div className="m-page">
      <MobileTopBar
        title="用量"
        subtitle="token 消耗与技能调用"
        actions={
          <button className="m-icon-btn" onClick={() => void load()} aria-label="刷新" disabled={loading}>
            ↻
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
          <div className="row" style={{ gap: 6 }}>
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
            <div className="stack">
              <div className="card-section-title">token 消耗</div>
              <div className="m-usage-stats">
                <MobileStat label="总 token" value={fmt(tokens.total)} unit="tok" highlight />
                <MobileStat label="输入" value={fmt(tokens.input)} unit="tok" />
                <MobileStat label="输出" value={fmt(tokens.output)} unit="tok" />
                <MobileStat
                  label="缓存命中率"
                  value={tokens.cache_ratio === null ? '—' : `${Math.round(tokens.cache_ratio * 100)}%`}
                />
                <MobileStat label="命中缓存" value={fmt(tokens.cache_read)} unit="tok" />
                <MobileStat label="写入缓存" value={fmt(tokens.cache_creation)} unit="tok" />
                <MobileStat label="统计消息" value={fmt(tokens.messages)} unit="条" />
                <MobileStat label="涉及对话" value={fmt(data.conversations)} unit="条" />
              </div>
            </div>

            {data.daily.length > 0 && (
              <div className="stack">
                <div className="card-section-title">每日消耗</div>
                <div className="m-usage-bars">
                  {data.daily.map((day) => (
                    <div className="m-usage-bar-row" key={day.date}>
                      <span className="dim small">{day.date.slice(5)}</span>
                      <span className="m-usage-bar-track">
                        <span
                          className="m-usage-bar-fill"
                          style={{ width: `${Math.max(2, (day.total / dailyMax) * 100)}%` }}
                        />
                      </span>
                      <span className="m-usage-bar-value">{compact(day.total)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {data.by_agent.length > 0 && (
              <div className="stack">
                <div className="card-section-title">按智能体</div>
                <div className="m-list">
                  {data.by_agent.map((agent) => (
                    <div className="m-item" key={agent.id}>
                      <span className="m-item-main">
                        <span className="m-item-title">
                          {agent.avatar} {agent.name}
                        </span>
                        <span className="m-item-sub">
                          {agent.type_name ?? agent.type_id ?? '未知内核'} · 输入 {fmt(agent.tokens.input)} · 输出{' '}
                          {fmt(agent.tokens.output)}
                        </span>
                      </span>
                      <span className="m-usage-num">
                        {fmt(agent.tokens.total)}
                        <span className="dim small"> tok</span>
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {data.by_conversation.length > 0 && (
              <div className="stack">
                <div className="card-section-title">
                  按对话（消耗最多的 {data.by_conversation.length} 条）
                </div>
                <div className="m-list">
                  {data.by_conversation.map((row) => (
                    <button
                      className="m-item"
                      key={row.id}
                      onClick={() => navigate(`/chat/${row.id}`)}
                      title="打开这条对话"
                    >
                      <span className="m-item-main">
                        <span className="m-item-title">{row.title}</span>
                        <span className="m-item-sub">
                          {row.agent_name ?? '—'}
                          {row.updated_at ? ` · ${formatListTime(row.updated_at)}` : ''}
                        </span>
                      </span>
                      <span className="m-usage-num">
                        {fmt(row.tokens.total)}
                        <span className="dim small"> tok</span>
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            <div className="stack">
              <div className="card-section-title">技能用量</div>
              <div className="m-usage-stats">
                <MobileStat label="调用总次数" value={fmt(skills.calls)} unit="次" highlight />
                <MobileStat label="涉及技能" value={fmt(skills.skills_used)} unit="个" />
              </div>
              <p className="dim small">
                技能调用按消息里的工具调用识别（如 Claude Code 的 Skill 工具），只统计所选时间范围内发生的调用；
                未声明调用形式的内核不会被计入。
              </p>

              {skills.top.length > 0 ? (
                <div className="m-list">
                  {skills.top.map((skill) => (
                    <button
                      className="m-item"
                      key={skill.id}
                      onClick={() => navigate('/skills')}
                      title="到技能页查看"
                    >
                      <span className="m-item-main">
                        <span className="m-item-title">{skill.name}</span>
                        <span className="m-item-sub">
                          {skill.type_name ?? skill.type_id}
                          {skill.source_label ? ` · ${skill.source_label}` : ''}
                        </span>
                      </span>
                      <span className="m-usage-num">
                        {fmt(skill.calls)}
                        <span className="dim small"> 次</span>
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="dim small">所选时间范围内没有技能调用。</p>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function MobileStat({
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
    <div className={`m-stat${highlight ? ' highlight' : ''}`}>
      <div className="m-stat-label">{label}</div>
      <div className="m-stat-value">
        {value}
        {unit && <span className="m-stat-unit">{unit}</span>}
      </div>
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
