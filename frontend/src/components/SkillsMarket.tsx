import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { MarketKernel, MarketSkill, MarketSkillList } from '../lib/types'

/** 排序方式：与后端 `sort_by` 对齐。 */
const SORTS = [
  { value: 'score', label: '综合评分' },
  { value: 'downloads', label: '下载量' },
  { value: 'updated', label: '最近更新' },
  { value: 'stars', label: '收藏数' },
]

const PAGE_SIZE = 24

/**
 * 技能市场：浏览并安装腾讯 SkillHub（skillhub.cn）上的技能。
 *
 * 安装后技能落在**当前用户**在对应内核下的技能库（按用户隔离），仍需到
 * 「智能体 → 技能」分配才会在运行时生效。
 * 组件本身只负责市场侧交互，安装成功通过 `onInstalled` 回给页面（用于刷新本地列表）。
 */
export function SkillsMarket({
  onInstalled,
  variant = 'desktop',
}: {
  onInstalled?: (message: string) => void | Promise<void>
  variant?: 'desktop' | 'mobile'
}) {
  const [data, setData] = useState<MarketSkillList | null>(null)
  const [kernels, setKernels] = useState<MarketKernel[]>([])
  const [kernel, setKernel] = useState('')
  const [query, setQuery] = useState('')
  const [sortBy, setSortBy] = useState('score')
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [installing, setInstalling] = useState<string | null>(null)
  // 已安装的市场技能，键是 `${内核}:${slug}` —— 同一技能在两个内核里是两条独立安装，
  const [installed, setInstalled] = useState<Record<string, string>>({})
  const timer = useRef<number | null>(null)

  const load = useCallback(
    async (opts: { page: number; keyword: string; sort: string }) => {
      setLoading(true)
      try {
        setData(
          await api.listMarketSkills({
            page: opts.page,
            page_size: PAGE_SIZE,
            sort_by: opts.sort,
            keyword: opts.keyword || undefined,
          }),
        )
        setError(null)
      } catch (e) {
        setError(describe(e))
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  const refreshInstalled = useCallback(async () => {
    try {
      const rows = await api.listSkills({ source_kind: 'market' })
      const map: Record<string, string> = {}
      for (const row of rows) {
        if (!row.available) continue // 已卸载（目录没了）的技能不算已安装
        if (!row.is_own) continue // 只看自己技能库里的安装；共享的老市场技能不算
        // 按「内核 + slug」记录：同一技能装到不同内核是各自独立的安装。
        map[installKey(row.type_id, row.slug)] = row.id
      }
      setInstalled(map)
    } catch {
      // 本地编目读不到不影响浏览市场，静默忽略。
    }
  }, [])

  useEffect(() => {
    void load({ page: 1, keyword: '', sort: 'score' })
    void refreshInstalled()
    api
      .marketKernels()
      .then((items) => {
        setKernels(items)
        setKernel((current) => current || items[0]?.type_id || '')
      })
      .catch((e) => setError(describe(e)))
  }, [load, refreshInstalled])

  const search = (value: string) => {
    setQuery(value)
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      setPage(1)
      void load({ page: 1, keyword: value, sort: sortBy })
    }, 350)
  }

  const changeSort = (value: string) => {
    setSortBy(value)
    setPage(1)
    void load({ page: 1, keyword: query, sort: value })
  }

  const goto = (next: number) => {
    setPage(next)
    void load({ page: next, keyword: query, sort: sortBy })
  }

  const install = async (skill: MarketSkill) => {
    if (!kernel) {
      setError('请先选择一个要安装到的内核')
      return
    }
    setInstalling(skill.slug)
    setNotice(null)
    try {
      const result = await api.installMarketSkill({ slug: skill.slug, type_id: kernel })
      await refreshInstalled()
      const hint = result.requires_api_key
        ? '（该技能需要第三方 API 密钥，记得在「设置 → 凭据」里配好）'
        : ''
      const message =
        `已把「${result.name}」挂载到 ${kernelLabel(kernels, kernel)}（技能文件全内核共用，重新安装即同步更新）` +
        `，请到「智能体 → 技能」分配给智能体后生效${hint}`
      setNotice(message)
      await onInstalled?.(message)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setInstalling(null)
    }
  }

  const uninstall = async (skill: MarketSkill) => {
    const skillId = installed[installKey(kernel, skill.slug)]
    if (!skillId) return
    setInstalling(skill.slug)
    setNotice(null)
    try {
      const result = await api.uninstallMarketSkill(skillId)
      await refreshInstalled()
      const still = result.remaining_kernels ?? []
      const message =
        still.length > 0
          ? `已从 ${kernelLabel(kernels, kernel)} 卸载「${skill.name}」，但仍挂载在 ${still.length} 个内核上`
          : `已卸载「${skill.name}」`
      setNotice(message)
      await onInstalled?.(message)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setInstalling(null)
    }
  }

  const header = (
    <div className="row wrap" style={{ gap: 10, alignItems: 'center' }}>
      <input
        className="input"
        style={{ flex: 1, minWidth: 180 }}
        value={query}
        placeholder="搜索腾讯 SkillHub（名称 / 说明）"
        onChange={(e) => search(e.target.value)}
      />
      <select className="select" value={sortBy} onChange={(e) => changeSort(e.target.value)}>
        {SORTS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {kernels.length > 0 && (
        <select
          className="select"
          value={kernel}
          onChange={(e) => setKernel(e.target.value)}
          title="安装后把技能挂载到哪个内核（文件全内核共用）"
        >
          {kernels.map((item) => (
            <option key={item.type_id} value={item.type_id}>
              安装到 {item.name}
            </option>
          ))}
        </select>
      )}
    </div>
  )

  const body = data?.items ?? []

  return (
    <div
      className="stack"
      style={{ padding: variant === 'mobile' ? 0 : '16px 22px', overflow: 'auto' }}
    >
      {header}
      <div className="dim small">
        技能来源：腾讯 SkillHub（skillhub.cn）。同一技能在技能库里只存一份文件，安装到多个内核时共用；
        更新（重新安装）后所有挂载它的内核同时生效。安装后到「智能体 → 技能」分配即可。
      </div>
      {error && <div className="banner err">{error}</div>}
      {notice && <div className="banner ok">{notice}</div>}

      {loading ? (
        <p className="dim small">正在加载技能市场…</p>
      ) : body.length === 0 ? (
        <div className="empty">
          <div className="empty-icon">🛒</div>
          <p>没有找到技能。换个关键词试试，或到 skillhub.cn 看看。</p>
        </div>
      ) : (
        <div className={`skills-market-cards ${variant === 'mobile' ? 'stack' : 'grid'}`}>
          {body.map((skill) => (
            <div className="card" key={skill.slug}>
              <div className="card-head">
                <SkillIcon url={skill.icon_url} name={skill.name} />
                <div style={{ minWidth: 0 }}>
                  <div className="card-title">{skill.name}</div>
                  <div className="card-sub">{skill.author ? `作者 ${skill.author}` : skill.owner ?? ''}</div>
                </div>
                <div className="card-actions">
                  {installed[installKey(kernel, skill.slug)] ? (
                    <button
                      className="btn danger"
                      disabled={installing === skill.slug}
                      onClick={() => void uninstall(skill)}
                    >
                      {installing === skill.slug ? '卸载中…' : '卸载'}
                    </button>
                  ) : (
                    <button
                      className="btn primary"
                      disabled={installing === skill.slug || !kernel}
                      onClick={() => void install(skill)}
                    >
                      {installing === skill.slug ? '安装中…' : '安装'}
                    </button>
                  )}
                </div>
              </div>

              <div className="row wrap" style={{ gap: 6 }}>
                {skill.version && <span className="pill">v{skill.version}</span>}
                {skill.category && <span className="pill">{skill.category}</span>}
                <span className="pill">⬇ {formatCount(skill.downloads)}</span>
                {skill.stars > 0 && <span className="pill">★ {skill.stars}</span>}
                {skill.verified && <span className="pill ok">官方认证</span>}
                {skill.security && (
                  <span className={`pill${skill.security.includes('安全') ? ' ok' : ' warn'}`}>
                    {skill.security}
                  </span>
                )}
                {skill.requires_api_key && <span className="pill warn">需要 API 密钥</span>}
                {installedElsewhere(installed, kernels, kernel, skill.slug).map((item) => (
                  <span className="pill" key={item.type_id}>
                    已装到 {item.name}
                  </span>
                ))}
              </div>

              <p className="card-sub" style={{ marginTop: 6, lineHeight: 1.6 }}>
                {skill.description || '（无描述）'}
              </p>

              <div className="row" style={{ gap: 10, marginTop: 'auto' }}>
                <a
                  className="btn ghost"
                  href={skill.url}
                  target="_blank"
                  rel="noreferrer"
                  style={{ marginLeft: 'auto' }}
                >
                  在市场查看 ↗
                </a>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="row" style={{ gap: 10, alignItems: 'center' }}>
        <span className="dim small">
          第 {data?.page ?? page} 页 · 共 {formatCount(data?.total ?? 0)} 个技能
        </span>
        <div className="spacer" />
        <button className="btn" disabled={page <= 1 || loading} onClick={() => goto(page - 1)}>
          上一页
        </button>
        <button className="btn" disabled={!data?.has_more || loading} onClick={() => goto(page + 1)}>
          下一页
        </button>
      </div>
    </div>
  )
}

function installKey(typeId: string, slug: string): string {
  return `${typeId}:${slug}`
}

/** 市场技能图标：有图就用图；没图或图挂了退回首字，卡片高度保持一致。 */
function SkillIcon({ url, name }: { url?: string | null; name: string }) {
  const src = normalizeIconUrl(url)
  const [broken, setBroken] = useState(false)

  useEffect(() => setBroken(false), [src])

  if (src && !broken) {
    return (
      <img
        className="skill-icon"
        src={src}
        alt=""
        loading="lazy"
        referrerPolicy="no-referrer"
        onError={() => setBroken(true)}
      />
    )
  }
  return (
    <div className="skill-icon skill-icon-fallback" aria-hidden="true">
      {Array.from(name.trim())[0]?.toUpperCase() || '🧩'}
    </div>
  )
}

/** 市场偶尔给相对路径；统一补成完整地址，`<img>` 才能直接加载。 */
function normalizeIconUrl(url?: string | null): string {
  const value = (url || '').trim()
  if (!value) return ''
  if (value.startsWith('//')) return `https:${value}`
  if (value.startsWith('/')) return `https://skillhub.cn${value}`
  return value
}

/** 除了当前选中内核之外，这个技能还装在了哪些内核上。 */
function installedElsewhere(
  installed: Record<string, string>,
  kernels: MarketKernel[],
  current: string,
  slug: string,
): MarketKernel[] {
  return kernels.filter((item) => item.type_id !== current && installed[installKey(item.type_id, slug)])
}

function kernelLabel(kernels: MarketKernel[], typeId: string): string {
  return kernels.find((item) => item.type_id === typeId)?.name ?? typeId
}

function formatCount(value: number): string {
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万`
  return String(value)
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
