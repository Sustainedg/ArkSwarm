import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useAuth } from '../components/AuthGate'
import { CollapsibleGroup } from '../components/CollapsibleGroup'
import { useExpandMap } from '../lib/collapse'
import type { Skill, SkillDetail } from '../lib/types'
import { SkillsMarket } from '../components/SkillsMarket'
import { MarkdownView } from '../components/MarkdownView'
import { stripFrontmatter } from '../lib/skillBody'

/**
 * 技能页：把各智能体内核的技能目录统一编目后集中展示。
 * 技能只能通过「智能体 → 技能」的分配被某个智能体使用，这里只负责查看与重新扫描。
 */
export function SkillsPage() {
  const [tab, setTab] = useState<'local' | 'market'>('local')
  const [skills, setSkills] = useState<Skill[]>([])
  const [query, setQuery] = useState('')
  const [sourceKind, setSourceKind] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<SkillDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [deleting, setDeleting] = useState(false)
  /** 按内核分组默认折叠：技能多的时候先给一份目录，要看谁点开谁。 */
  const { isExpanded, toggle } = useExpandMap()

  const reload = async (params?: { source_kind?: string; q?: string }) => {
    try {
      // 已卸载的市场技能不该再出现；后端会过滤，这里再兜一层（旧数据里可能还留着失效行）。
      const rows = await api.listSkills(params)
      setSkills(rows.filter((skill) => !(skill.is_market && !skill.available)))
      setError(null)
    } catch (e) {
      setError(describe(e))
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  useEffect(() => {
    if (!selected) {
      setDetail(null)
      return
    }
    let cancelled = false
    api
      .getSkill(selected)
      .then((data) => {
        if (!cancelled) setDetail(data)
      })
      .catch((e) => {
        if (!cancelled) setError(describe(e))
      })
    return () => {
      cancelled = true
    }
  }, [selected])

  const rescan = async () => {
    setScanning(true)
    try {
      const result = await api.rescanSkills()
      const summary =
        `扫描完成：新增 ${result.added} · 更新 ${result.updated} · ` +
        `目录已消失 ${result.missing} · 共 ${result.total}`
      setNotice(summary)
      await reload({ source_kind: sourceKind || undefined, q: query || undefined })
    } catch (e) {
      setError(describe(e))
    } finally {
      setScanning(false)
    }
  }

  const applyFilter = async (next: { source_kind?: string; q?: string }) => {
    await reload(next)
    if (selected && !skills.length) setSelected(null)
  }

  const removeSkill = async (skill: SkillDetail) => {
    if (
      !confirm(
        `删除技能「${skill.name}」？\n会删除它的源目录，并解除所有智能体对它的分配。`,
      )
    ) {
      return
    }
    setDeleting(true)
    setNotice(null)
    try {
      const result = await api.deleteSkill(skill.id)
      setNotice(`已删除「${result.name}」`)
      setSelected(null)
      setDetail(null)
      await reload({ source_kind: sourceKind || undefined, q: query || undefined })
    } catch (e) {
      setError(describe(e))
    } finally {
      setDeleting(false)
    }
  }

  const groups = groupByType(skills)

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">技能</h1>
          <p className="page-desc">
            各智能体内核自带的技能在这里统一编目。技能通过「智能体分配」生效；
            腾讯 SkillHub 安装的技能与自行放进技能目录的技能都可以删除，
            内核自带技能与已安装插件里的技能只读。
          </p>
        </div>
        <div className="spacer" />
        <div className="tabs">
          <button
            className={`tab${tab === 'local' ? ' active' : ''}`}
            onClick={() => setTab('local')}
          >
            本地技能
          </button>
          <button
            className={`tab${tab === 'market' ? ' active' : ''}`}
            onClick={() => setTab('market')}
          >
            技能市场
          </button>
        </div>
        {tab === 'local' && (
          <>
          <input
            className="input"
            style={{ maxWidth: 220 }}
            value={query}
            placeholder="搜索技能名或说明"
            onChange={(e) => {
              setQuery(e.target.value)
              void applyFilter({ q: e.target.value, source_kind: sourceKind || undefined })
            }}
          />
          <select
            className="select"
            style={{ maxWidth: 170 }}
            value={sourceKind}
            onChange={(e) => {
              setSourceKind(e.target.value)
              void applyFilter({ source_kind: e.target.value || undefined, q: query || undefined })
            }}
          >
            <option value="">全部来源</option>
            <option value="user">个人技能</option>
            <option value="project">项目技能</option>
            <option value="plugin">插件技能</option>
            <option value="system">内核系统技能</option>
            <option value="market">腾讯 SkillHub</option>
          </select>
          <button className="btn" onClick={() => void rescan()} disabled={scanning}>
            {scanning ? '扫描中…' : '重新扫描'}
          </button>
          </>
        )}
      </header>

      {tab === 'market' ? (
        <SkillsMarket
          onInstalled={async (message) => {
            setNotice(message)
            await reload({ source_kind: sourceKind || undefined, q: query || undefined })
          }}
        />
      ) : (
      <div className="skill-shell">
        <div className="skill-list">
          {error && <div className="banner err">{error}</div>}
          {notice && <div className="banner ok">{notice}</div>}
          {skills.length === 0 && !error && (
            <div className="empty">
              <div className="empty-icon">🧩</div>
              <p>没有扫描到技能。点右上角「重新扫描」试试。</p>
            </div>
          )}
          {groups.map((group) => (
            <CollapsibleGroup
              key={group.typeId}
              title={group.typeName}
              count={group.items.length}
              expanded={isExpanded(group.typeId)}
              onToggle={() => toggle(group.typeId)}
            >
              {group.items.map((skill) => (
                <div
                  key={skill.id}
                  className={`chat-item${skill.id === selected ? ' active' : ''}`}
                  onClick={() => setSelected(skill.id)}
                >
                  <div className="chat-item-title">{skill.name}</div>
                  <div className="chat-item-meta">
                    <span className={`pill${skill.available ? '' : ' err'}`}>
                      {skill.available ? skill.source_label : '目录已消失'}
                    </span>
                    {skill.used_by_agents > 0 && (
                      <span className="pill brand">被 {skill.used_by_agents} 个智能体使用</span>
                    )}
                  </div>
                </div>
              ))}
            </CollapsibleGroup>
          ))}
        </div>

        <div className="skill-pane">
          {detail ? (
            <SkillDetailPane
              skill={detail}
              deleting={deleting}
              onDelete={() => void removeSkill(detail)}
            />
          ) : (
            <div className="empty">
              <div className="empty-icon">📖</div>
              <p>左侧选择一个技能，查看它的完整描述。</p>
            </div>
          )}
        </div>
      </div>
      )}
    </>
  )
}

function SkillDetailPane({
  skill,
  deleting,
  onDelete,
}: {
  skill: SkillDetail
  deleting: boolean
  onDelete: () => void
}) {
  const { isAdmin } = useAuth()
  const kernel = skill.type_name ?? skill.type_id
  return (
    <div className="skill-detail">
      <header className="skill-detail-hero">
        <div className="skill-detail-eyebrow">
          <span className="pill brand">{skill.source_label}</span>
          <span className="skill-detail-kernel">{kernel}</span>
          {!skill.available && <span className="pill err">目录已消失</span>}
        </div>
        <h2 className="skill-detail-title">{skill.name}</h2>
        {skill.description && <p className="skill-detail-desc">{skill.description}</p>}
        <div className="skill-detail-chips">
          <span className="skill-chip">
            {skill.file_count} 个文件 · {formatBytes(skill.total_bytes)}
          </span>
          <span className="skill-chip">
            {skill.agents.length > 0 ? `被 ${skill.agents.length} 个智能体使用` : '尚未分配'}
          </span>
          {skill.version && <span className="skill-chip">v{skill.version}</span>}
          {skill.author && <span className="skill-chip">作者 {skill.author}</span>}
          {skill.is_market && skill.source_url && (
            <a className="skill-chip skill-chip-link" href={skill.source_url} target="_blank" rel="noreferrer">
              去腾讯 SkillHub 查看 ↗
            </a>
          )}
        </div>
        {isAdmin && skill.deletable && (
          <div className="skill-detail-actions">
            <button className="btn danger" onClick={onDelete} disabled={deleting}>
              {deleting ? '删除中…' : '删除技能'}
            </button>
            <span>会删除它的源目录，并解除所有智能体对它的分配。</span>
          </div>
        )}
      </header>

      {skill.note && <div className="banner info">{skill.note}</div>}

      <div className="skill-detail-columns">
        <section className="skill-detail-card">
          <div className="skill-detail-card-head">
            <h3 className="skill-detail-card-title">使用它的智能体</h3>
            <span className="skill-detail-count">{skill.agents.length}</span>
          </div>
          {skill.agents.length > 0 ? (
            <div className="skill-detail-agents">
              {skill.agents.map((agent) => (
                <span className="skill-agent" key={agent.id}>
                  <span className="skill-agent-avatar">{agent.avatar || '🤖'}</span>
                  <span className="skill-agent-name">{agent.name}</span>
                </span>
              ))}
            </div>
          ) : (
            <p className="skill-detail-empty">尚未分配给任何智能体，当前不会被使用。</p>
          )}
        </section>

        <section className="skill-detail-card">
          <div className="skill-detail-card-head">
            <h3 className="skill-detail-card-title">基本信息</h3>
          </div>
          <dl className="skill-detail-kv">
            <dt>所属内核</dt>
            <dd>{kernel}</dd>
            <dt>来源</dt>
            <dd>{skill.source_label}</dd>
            <dt>版本</dt>
            <dd>{skill.version || '—'}</dd>
            <dt>作者</dt>
            <dd>{skill.author || '—'}</dd>
            <dt>标识</dt>
            <dd className="mono-sm">{skill.id}</dd>
          </dl>
        </section>
      </div>

      <section className="skill-detail-card">
        <div className="skill-detail-card-head">
          <h3 className="skill-detail-card-title">完整描述</h3>
          <span className="skill-detail-card-sub">SKILL.md</span>
        </div>
        {skill.body ? (
          <>
            <div className="skill-detail-body">
              <MarkdownView text={stripFrontmatter(skill.body)} />
            </div>
            {skill.body_truncated && (
              <p className="skill-detail-empty">内容过长，已截断显示。</p>
            )}
          </>
        ) : (
          <p className="skill-detail-empty">（SKILL.md 为空或不可读）</p>
        )}
      </section>

      {skill.when_to_use && (
        <section className="skill-detail-card">
          <div className="skill-detail-card-head">
            <h3 className="skill-detail-card-title">适用场景</h3>
          </div>
          <p className="skill-detail-text">{skill.when_to_use}</p>
        </section>
      )}

      <div className="skill-detail-columns">
        <section className="skill-detail-card">
          <div className="skill-detail-card-head">
            <h3 className="skill-detail-card-title">来源目录</h3>
          </div>
          <dl className="skill-detail-kv">
            <dt>路径</dt>
            <dd className="mono-sm skill-detail-path">{skill.path}</dd>
            <dt>内容指纹</dt>
            <dd className="mono-sm">{skill.fingerprint || '—'}</dd>
            <dt>文件数</dt>
            <dd>
              {skill.file_count} 个 · {formatBytes(skill.total_bytes)}
            </dd>
          </dl>
        </section>

        <section className="skill-detail-card">
          <div className="skill-detail-card-head">
            <h3 className="skill-detail-card-title">允许的工具</h3>
          </div>
          {skill.allowed_tools.length > 0 ? (
            <div className="chips">
              {skill.allowed_tools.map((tool) => (
                <span className="pill mono-sm" key={tool}>
                  {tool}
                </span>
              ))}
            </div>
          ) : (
            <p className="skill-detail-empty">未声明允许的工具。</p>
          )}
        </section>
      </div>

      {skill.files.length > 0 && (
        <section className="skill-detail-card">
          <div className="skill-detail-card-head">
            <h3 className="skill-detail-card-title">文件清单</h3>
            <span className="skill-detail-count">{skill.files.length}</span>
          </div>
          <ul className="skill-file-list">
            {skill.files.map((file) => (
              <li key={file.path}>
                <span className="skill-file-name">{file.path}</span>
                <span className="skill-file-size">{formatBytes(file.size)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

function groupByType(skills: Skill[]): { typeId: string; typeName: string; items: Skill[] }[] {
  const groups = new Map<string, { typeId: string; typeName: string; items: Skill[] }>()
  for (const skill of skills) {
    const group = groups.get(skill.type_id) ?? {
      typeId: skill.type_id,
      typeName: skill.type_name ?? skill.type_id,
      items: [],
    }
    group.items.push(skill)
    groups.set(skill.type_id, group)
  }
  return [...groups.values()]
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
