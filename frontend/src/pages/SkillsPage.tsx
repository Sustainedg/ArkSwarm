import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { CollapsibleGroup } from '../components/CollapsibleGroup'
import { useExpandMap } from '../lib/collapse'
import type { Skill, SkillDetail } from '../lib/types'
import { SkillsMarket } from '../components/SkillsMarket'

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
  /** 按内核分组默认折叠：技能多的时候先给一份目录，要看谁点开谁。 */
  const { isExpanded, toggle } = useExpandMap()

  const reload = async (params?: { source_kind?: string; q?: string }) => {
    try {
      setSkills(await api.listSkills(params))
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

  const groups = groupByType(skills)

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">技能</h1>
          <p className="page-desc">
            各智能体内核自带的技能在这里统一编目。技能通过「智能体分配」生效，
            未分配的技能不会被任何智能体使用。
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
            <option value="market">市场技能</option>
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
          {detail ? <SkillDetailPane skill={detail} /> : (
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

function SkillDetailPane({ skill }: { skill: SkillDetail }) {
  return (
    <div className="stack" style={{ padding: 22, overflow: 'auto' }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <span className="pill brand">{skill.source_label}</span>
        <span className="pill mono-sm">{skill.type_name ?? skill.type_id}</span>
        <span className="pill mono-sm">{skill.id}</span>
        {!skill.available && <span className="pill err">目录已消失</span>}
        {skill.version && <span className="pill">v{skill.version}</span>}
        {skill.author && <span className="pill">作者 {skill.author}</span>}
      </div>

      {skill.note && <div className="banner info">{skill.note}</div>}

      {skill.agents.length > 0 ? (
        <div className="chips">
          {skill.agents.map((agent) => (
            <span className="pill ok" key={agent.id}>
              {agent.avatar} {agent.name}
            </span>
          ))}
        </div>
      ) : (
        <div className="card-sub">尚未分配给任何智能体，当前不会被使用。</div>
      )}

      <div>
        <div className="card-section-title" style={{ marginBottom: 6 }}>
          完整描述
        </div>
        <div className="skill-body">
          {skill.body || '（SKILL.md 为空或不可读）'}
          {skill.body_truncated && <div className="dim small">… 内容过长，已截断显示。</div>}
        </div>
      </div>

      <div>
        <div className="card-section-title" style={{ marginBottom: 6 }}>
          元数据
        </div>
        <dl className="kv">
          <dt>来源目录</dt>
          <dd className="mono-sm">{skill.path}</dd>
          <dt>内容指纹</dt>
          <dd className="mono-sm">{skill.fingerprint || '—'}</dd>
          <dt>文件数</dt>
          <dd>
            {skill.file_count} 个 · {formatBytes(skill.total_bytes)}
          </dd>
          {skill.when_to_use && (
            <>
              <dt>适用场景</dt>
              <dd>{skill.when_to_use}</dd>
            </>
          )}
          {skill.allowed_tools.length > 0 && (
            <>
              <dt>允许的工具</dt>
              <dd className="mono-sm">{skill.allowed_tools.join(' · ')}</dd>
            </>
          )}
        </dl>
      </div>

      {skill.files.length > 0 && (
        <div>
          <div className="card-section-title" style={{ marginBottom: 6 }}>
            文件清单
          </div>
          <dl className="kv">
            {skill.files.map((file) => (
              <div key={file.path} style={{ display: 'contents' }}>
                <dt className="mono-sm">{file.path}</dt>
                <dd className="dim small">{formatBytes(file.size)}</dd>
              </div>
            ))}
          </dl>
        </div>
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
