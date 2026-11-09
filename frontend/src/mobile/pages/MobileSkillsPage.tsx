import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../../lib/api'
import type { Skill, SkillDetail } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

const SOURCE_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'user', label: '个人技能' },
  { value: 'project', label: '项目技能' },
  { value: 'plugin', label: '插件技能' },
  { value: 'system', label: '内核系统技能' },
]

/** 技能：各内核自带技能的统一编目视图；技能只有被角色分配后才生效。 */
export function MobileSkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [query, setQuery] = useState('')
  const [sourceKind, setSourceKind] = useState('')
  const [selected, setSelected] = useState<SkillDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(async (params?: { source_kind?: string; q?: string }) => {
    try {
      setSkills(await api.listSkills(params))
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

  const rescan = async () => {
    setScanning(true)
    setNotice(null)
    try {
      const result = await api.rescanSkills()
      setNotice(`扫描完成：新增 ${result.added}、更新 ${result.updated}、目录已消失 ${result.missing}，共 ${result.total}`)
      await reload({ source_kind: sourceKind || undefined, q: query || undefined })
    } catch (e) {
      setError(describeError(e))
    } finally {
      setScanning(false)
    }
  }

  const openDetail = async (skill: Skill) => {
    setError(null)
    try {
      setSelected(await api.getSkill(skill.id))
    } catch (e) {
      setError(describeError(e))
    }
  }

  // 按内核分组展示：同类技能放在一起，方便按内核核对。
  const groups = useMemo(() => {
    const map = new Map<string, { typeName: string; items: Skill[] }>()
    for (const skill of skills) {
      const key = skill.type_id
      if (!map.has(key)) map.set(key, { typeName: skill.type_name ?? skill.type_id, items: [] })
      map.get(key)!.items.push(skill)
    }
    return [...map.entries()]
  }, [skills])

  return (
    <div className="m-page">
      <MobileTopBar
        title="技能"
        subtitle="统一编目 · 通过角色分配生效"
        actions={
          <button className="m-icon-btn" onClick={() => void rescan()} disabled={scanning} aria-label="重新扫描">
            {scanning ? '◌' : '↻'}
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          <input
            className="input"
            value={query}
            placeholder="搜索技能名或说明"
            onChange={(event) => {
              setQuery(event.target.value)
              void reload({ q: event.target.value, source_kind: sourceKind || undefined })
            }}
          />
          <select
            className="select"
            value={sourceKind}
            onChange={(event) => {
              setSourceKind(event.target.value)
              void reload({ source_kind: event.target.value || undefined, q: query || undefined })
            }}
          >
            {SOURCE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>

          {error && <div className="banner err">{error}</div>}
          {notice && <div className="banner info">{notice}</div>}

          {loading ? (
            <p className="dim small">正在加载…</p>
          ) : skills.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">🧩</div>
              <p>没有扫描到技能</p>
              <p className="dim small">点右上角「↻」重新扫描各内核的技能目录。</p>
            </div>
          ) : (
            groups.map(([typeId, group]) => (
              <div className="stack" key={typeId} style={{ gap: 8 }}>
                <div className="card-section-title">
                  {group.typeName}（{group.items.length}）
                </div>
                {group.items.map((skill) => (
                  <button key={skill.id} className="m-item" onClick={() => void openDetail(skill)}>
                    <span className="m-item-main">
                      <span className="m-item-title">{skill.name}</span>
                      <span className="m-item-sub">{skill.description || '（无描述）'}</span>
                    </span>
                    <span className="pill">{skill.available ? skill.source_label : '目录已消失'}</span>
                    <span className="m-item-chevron">›</span>
                  </button>
                ))}
              </div>
            ))
          )}
        </div>
      </div>

      {selected && (
        <MobileSheet full title={`技能 · ${selected.name}`} onClose={() => setSelected(null)}>
          <div className="row wrap" style={{ gap: 6 }}>
            <span className="pill brand">{selected.type_name ?? selected.type_id}</span>
            <span className="pill">{selected.source_label}</span>
            {!selected.available && <span className="pill err">目录已消失</span>}
            <span className="pill">{selected.file_count} 个文件</span>
          </div>

          <p className="muted" style={{ fontSize: 13.5 }}>{selected.description || '（无描述）'}</p>

          {selected.when_to_use && (
            <div className="field">
              <span className="field-label">适用场景</span>
              <p className="dim small" style={{ lineHeight: 1.6 }}>{selected.when_to_use}</p>
            </div>
          )}

          <div className="field">
            <span className="field-label">被哪些角色使用</span>
            {selected.roles.length === 0 ? (
              <p className="dim small">还没有角色分配这个技能。</p>
            ) : (
              <div className="row wrap" style={{ gap: 6 }}>
                {selected.roles.map((role) => (
                  <span className="pill" key={role.id}>
                    {role.avatar} {role.name}
                  </span>
                ))}
              </div>
            )}
          </div>

          {selected.allowed_tools.length > 0 && (
            <div className="field">
              <span className="field-label">允许的工具</span>
              <div className="row wrap" style={{ gap: 6 }}>
                {selected.allowed_tools.map((tool) => (
                  <span className="pill" key={tool}>
                    {tool}
                  </span>
                ))}
              </div>
            </div>
          )}

          {selected.body && (
            <div className="field">
              <span className="field-label">
                SKILL.md{selected.body_truncated ? '（已截断）' : ''}
              </span>
              <div className="skill-body">{selected.body}</div>
            </div>
          )}
        </MobileSheet>
      )}
    </div>
  )
}
