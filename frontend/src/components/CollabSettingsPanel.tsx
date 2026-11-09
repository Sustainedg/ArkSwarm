/**
 * 「协作设置」页签：左栏协作组列表，中间是画布编排器，右侧是属性面板。
 *
 * 编辑的是**一张完整的图**：拖动/连线只改本地草稿，点「保存」才整图提交（`PUT /teams/{id}`）。
 * 每次改动都会防抖地调一次 `POST /teams/{id}/validate`，把「人话提示」实时显示出来 ——
 * 校验在服务端（与运行时同一套规则），前端不重复实现一遍。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { CollabCanvas, type CollabSelection } from './CollabCanvas'
import { NodeInspector, type TeamDraft } from './NodeInspector'
import { Modal } from './Modal'
import { api, ApiError } from '../lib/api'
import type { AgentType, CollabBudget, CollabEdge, CollabIssue, CollabNode, CollabTeam, Role } from '../lib/types'

interface Props {
  teamId?: string
  teams: CollabTeam[]
  roles: Role[]
  agentTypes: AgentType[]
  onOpenTeam: (teamId: string | null) => void
  onTeamsChanged: () => void | Promise<void>
}

const DEFAULT_BUDGET: CollabBudget = {
  max_steps: 60,
  max_tickets: 30,
  max_rounds_per_ticket: 3,
  max_tokens: 0,
  timeout_seconds: 3600,
}

/** 后端 BudgetSpec 有 ge 约束：这里按同一套下限夹紧，避免保存时才 422 */
function clampBudget(budget: CollabBudget): CollabBudget {
  return {
    max_steps: Math.max(1, budget.max_steps),
    max_tickets: Math.max(1, budget.max_tickets),
    max_rounds_per_ticket: Math.max(1, budget.max_rounds_per_ticket),
    max_tokens: Math.max(0, budget.max_tokens),
    timeout_seconds: Math.max(30, budget.timeout_seconds),
  }
}

function toDraft(team: CollabTeam): TeamDraft {
  return {
    name: team.name,
    description: team.description ?? '',
    entry_key: team.entry_key ?? null,
    workspace_dir: team.workspace_dir ?? '',
    budget: { ...DEFAULT_BUDGET, ...team.budget },
    nodes: team.graph.nodes.map((node) => ({ ...node })),
    // 旧数据里没有 max_rounds，补 0（前端不依赖后端一定回传新字段）
    edges: team.graph.edges.map((edge) => ({ ...edge, max_rounds: edge.max_rounds ?? 0 })),
  }
}

/** 节点 key 必须满足后端的 `^[a-zA-Z0-9][a-zA-Z0-9_-]*$` */
function sanitizeKey(raw: string): string {
  const cleaned = raw.replace(/[^a-zA-Z0-9_-]/g, '-').replace(/^[^a-zA-Z0-9]+/, '')
  return cleaned.slice(0, 64)
}

function defaultKey(duty: string, taken: Set<string>): string {
  const base = duty === 'reviewer' ? 'review' : duty === 'coordinator' ? 'lead' : 'dev'
  if (!taken.has(base)) return base
  let index = 2
  while (taken.has(`${base}-${index}`)) index += 1
  return `${base}-${index}`
}

export function CollabSettingsPanel({ teamId, teams, roles, agentTypes, onOpenTeam, onTeamsChanged }: Props) {
  const [draft, setDraft] = useState<TeamDraft | null>(null)
  const [selection, setSelection] = useState<CollabSelection | null>(null)
  const [issues, setIssues] = useState<CollabIssue[]>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const versionRef = useRef(0)

  const loadTeam = useCallback(async (id: string) => {
    try {
      const loaded = await api.getCollabTeam(id)
      setDraft(toDraft(loaded))
      setIssues([...loaded.validation.errors, ...loaded.validation.warnings])
      setDirty(false)
      setSelection(null)
      setError(null)
    } catch (e) {
      setError(describe(e))
      setDraft(null)
    }
  }, [])

  useEffect(() => {
    if (teamId) void loadTeam(teamId)
    else {
      setDraft(null)
      setSelection(null)
      setIssues([])
    }
  }, [teamId, loadTeam])

  // 实时校验：改动后防抖 500ms 调一次服务端校验（与运行时同一套规则）
  useEffect(() => {
    if (!teamId || !draft || !dirty) return
    const version = ++versionRef.current
    const timer = window.setTimeout(async () => {
      try {
        const validation = await api.validateCollabTeam(teamId, {
          graph: { nodes: draft.nodes, edges: draft.edges },
          entry_key: draft.entry_key,
        })
        if (version !== versionRef.current) return
        setIssues([...validation.errors, ...validation.warnings])
        setError(null)
      } catch (e) {
        if (version !== versionRef.current) return
        setError(describe(e))
      }
    }, 500)
    return () => window.clearTimeout(timer)
  }, [teamId, draft, dirty])

  const patchTeam = (patch: Partial<TeamDraft>) => {
    setDraft((prev) => (prev ? { ...prev, ...patch } : prev))
    setDirty(true)
  }

  const patchNode = (key: string, patch: Partial<CollabNode>) => {
    setDraft((prev) =>
      prev ? { ...prev, nodes: prev.nodes.map((node) => (node.key === key ? { ...node, ...patch } : node)) } : prev,
    )
    setDirty(true)
  }

  const patchEdge = (id: string, patch: Partial<CollabEdge>) => {
    setDraft((prev) =>
      prev ? { ...prev, edges: prev.edges.map((edge) => (edge.id === id ? { ...edge, ...patch } : edge)) } : prev,
    )
    setDirty(true)
  }

  const renameNode = (oldKey: string, rawKey: string) => {
    const key = sanitizeKey(rawKey)
    if (!key || key === oldKey) return
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            entry_key: prev.entry_key === oldKey ? key : prev.entry_key,
            nodes: prev.nodes.map((node) => (node.key === oldKey ? { ...node, key } : node)),
            edges: prev.edges.map((edge) => ({
              ...edge,
              from_key: edge.from_key === oldKey ? key : edge.from_key,
              to_key: edge.to_key === oldKey ? key : edge.to_key,
            })),
          }
        : prev,
    )
    setSelection({ kind: 'node', id: key })
    setDirty(true)
  }

  const deleteNode = (key: string) => {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            entry_key: prev.entry_key === key ? null : prev.entry_key,
            nodes: prev.nodes.filter((node) => node.key !== key),
            edges: prev.edges.filter((edge) => edge.from_key !== key && edge.to_key !== key),
          }
        : prev,
    )
    setSelection(null)
    setDirty(true)
  }

  const deleteEdge = (id: string) => {
    setDraft((prev) => (prev ? { ...prev, edges: prev.edges.filter((edge) => edge.id !== id) } : prev))
    setSelection(null)
    setDirty(true)
  }

  /** 从角色抽屉添加一个岗位节点（点选或拖放都会走到这里） */
  const addNode = (role: Role, at?: { x: number; y: number }) => {
    setDraft((prev) => {
      if (!prev) return prev
      const taken = new Set(prev.nodes.map((node) => node.key))
      const index = prev.nodes.length
      // 第一个节点默认当入口（协调者），省得用户还得手动指一次
      const duty = prev.nodes.length === 0 ? 'coordinator' : 'engineer'
      const key = defaultKey(duty, taken)
      const node: CollabNode = {
        key,
        role_id: role.id,
        label: role.name,
        duty,
        instructions: '',
        capability_scope: '',
        workspace_scope: null,
        output_contract: [],
        options_override: {},
        max_rounds: 0,
        x: at?.x ?? 40 + (index % 4) * 230,
        y: at?.y ?? 40 + Math.floor(index / 4) * 150,
      }
      const entry_key = prev.entry_key ?? (prev.nodes.length === 0 ? key : null)
      return { ...prev, nodes: [...prev.nodes, node], entry_key }
    })
    setDirty(true)
  }

  const save = async () => {
    if (!teamId || !draft) return
    setSaving(true)
    setError(null)
    try {
      const saved = await api.saveCollabTeam(teamId, {
        name: draft.name.trim() || '未命名协作组',
        description: draft.description,
        entry_key: draft.entry_key,
        workspace_dir: draft.workspace_dir.trim() || null,
        graph: { nodes: draft.nodes, edges: draft.edges },
        budget: clampBudget(draft.budget),
      })
      setDraft(toDraft(saved))
      setIssues([...saved.validation.errors, ...saved.validation.warnings])
      setDirty(false)
      await onTeamsChanged()
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  const createTeam = async () => {
    try {
      const created = await api.createCollabTeam({ name: newName.trim() || '新建协作组' })
      setCreating(false)
      setNewName('')
      await onTeamsChanged()
      onOpenTeam(created.id)
    } catch (e) {
      setError(describe(e))
    }
  }

  const errors = issues.filter((issue) => !issue.code.startsWith('warn'))
  const warnings = issues.filter((issue) => issue.code.startsWith('warn'))

  return (
    <div className="collab-settings">
      <aside className="collab-list">
        <button className="btn primary" style={{ justifyContent: 'center' }} onClick={() => setCreating(true)}>
          + 新建协作组
        </button>
        {teams.map((item) => (
          <button
            key={item.id}
            className={`collab-item${item.id === teamId ? ' active' : ''}`}
            onClick={() => onOpenTeam(item.id)}
          >
            <div className="chat-item-title">{item.name}</div>
            <div className="chat-item-meta">
              <span className={`pill ${item.validation.ok ? 'ok' : 'err'}`}>
                {item.validation.ok ? '可运行' : `${item.validation.errors.length} 个问题`}
              </span>
              <span>{item.node_count} 个岗位</span>
            </div>
          </button>
        ))}
        {teams.length === 0 && (
          <p className="dim small" style={{ padding: '14px 8px', lineHeight: 1.6 }}>
            还没有协作组。新建一个，然后把角色拖到画布上连起来。
          </p>
        )}
      </aside>

      {draft && teamId ? (
        <div className="collab-editor">
          <div className="collab-toolbar">
            <div className="collab-role-drawer">
              <span className="dim small">角色</span>
              <div className="collab-role-chips">
                {roles.map((role) => (
                  <button
                    key={role.id}
                    className="collab-role-chip"
                    draggable
                    title={`拖到画布上，或点击直接添加`}
                    onDragStart={(event) => event.dataTransfer.setData('text/collab-role', role.id)}
                    onClick={() => addNode(role)}
                  >
                    <span>{role.avatar}</span>
                    <span>{role.name}</span>
                  </button>
                ))}
                {roles.length === 0 && <span className="dim small">还没有角色，请先到「角色」页创建</span>}
              </div>
            </div>
            <div className="spacer" style={{ flex: 1 }} />
            <span className={`pill ${errors.length === 0 ? 'ok' : 'err'}`}>
              {errors.length === 0 ? '图可以跑' : `${errors.length} 个错误`}
            </span>
            {warnings.length > 0 && <span className="pill warn">{warnings.length} 条提醒</span>}
            {dirty && <span className="pill">未保存</span>}
            <button className="btn sm" onClick={() => teamId && void loadTeam(teamId)} disabled={saving}>
              放弃改动
            </button>
            <button className="btn primary sm" onClick={() => void save()} disabled={saving || !dirty}>
              {saving ? '保存中…' : '保存'}
            </button>
          </div>

          <div className="collab-workbench">
            <CollabCanvas
              nodes={draft.nodes}
              edges={draft.edges}
              roles={roles}
              entryKey={draft.entry_key}
              selected={selection}
              issues={issues}
              onChange={(graph) => {
                setDraft((prev) => (prev ? { ...prev, nodes: graph.nodes, edges: graph.edges } : prev))
                setDirty(true)
              }}
              onSelect={setSelection}
              onDropRole={(roleId, at) => {
                const role = roles.find((item) => item.id === roleId)
                if (role) addNode(role, at)
              }}
            />
            <NodeInspector
              draft={draft}
              selection={selection}
              roles={roles}
              agentTypes={agentTypes}
              issues={issues}
              onPatchTeam={patchTeam}
              onPatchNode={patchNode}
              onPatchEdge={patchEdge}
              onRenameNode={renameNode}
              onDeleteNode={deleteNode}
              onDeleteEdge={deleteEdge}
            />
          </div>

          {(errors.length > 0 || warnings.length > 0) && (
            <div className="collab-issues">
              {[...errors, ...warnings].slice(0, 6).map((issue, index) => (
                <div key={`${issue.code}-${index}`} className={`banner ${issue.code.startsWith('warn') ? 'info' : 'err'}`}>
                  {issue.message}
                </div>
              ))}
            </div>
          )}
          {error && <div className="banner err" style={{ margin: '10px 14px' }}>{error}</div>}
        </div>
      ) : (
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="empty-icon">🤝</div>
          <p>选择一个协作组开始编排，或新建一个</p>
          <p className="dim small" style={{ marginTop: 8 }}>
            画布上每个节点 = 一个角色在这次协作里的岗位
          </p>
          <button className="btn primary" style={{ marginTop: 14 }} onClick={() => setCreating(true)}>
            + 新建协作组
          </button>
        </div>
      )}

      {creating && (
        <Modal
          title="新建协作组"
          onClose={() => setCreating(false)}
          footer={
            <>
              <button className="btn ghost" onClick={() => setCreating(false)}>
                取消
              </button>
              <button className="btn primary" onClick={() => void createTeam()}>
                创建
              </button>
            </>
          }
        >
          <label className="field">
            <span className="field-label">名称</span>
            <input
              className="input"
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && void createTeam()}
              placeholder="例如：客户端研发组"
            />
          </label>
          <span className="field-help">创建后进入画布：从上方角色抽屉拖入（或点选）角色，再从节点右侧圆点拖到下一个节点连线。</span>
        </Modal>
      )}
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
