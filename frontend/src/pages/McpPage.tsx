import { useEffect, useMemo, useState } from 'react'
import { McpAssignPanel } from '../components/McpAssignPanel'
import { McpServerForm } from '../components/McpServerForm'
import { transportLabel } from '../components/McpPicker'
import { Modal } from '../components/Modal'
import { api, ApiError } from '../lib/api'
import type { AgentProfile, McpServer, McpServerDetail } from '../lib/types'

/** 平台通用服务器与内核无关，列表里单独归到这一组。 */
const PLATFORM_GROUP = '__platform__'

/**
 * MCP 页：把平台新建的与各内核配置里的 MCP 服务器统一编目。
 *
 * 和技能一样，服务器只有被「智能体分配」后才会生效：这里负责编目与维护
 * （新建 / 编辑 / 停用 / 删除 + 扫描本机已有配置），分配在智能体配置里或本页的
 * 「分配给智能体」里做。
 */
export function McpPage() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [query, setQuery] = useState('')
  const [sourceKind, setSourceKind] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<McpServerDetail | null>(null)
  // undefined＝关闭弹层；null＝新建；对象＝编辑
  const [editing, setEditing] = useState<McpServer | null | undefined>(undefined)
  const [assigning, setAssigning] = useState<McpServerDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [loading, setLoading] = useState(true)

  const reload = async (params?: { source_kind?: string; q?: string }) => {
    try {
      const [list, agentList] = await Promise.all([
        api.listMcpServers(params),
        api.listAgents(),
      ])
      setServers(list)
      setAgents(agentList)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
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
      .getMcpServer(selected)
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

  const refresh = () => reload({ source_kind: sourceKind || undefined, q: query || undefined })

  const rescan = async () => {
    setScanning(true)
    try {
      const result = await api.rescanMcpServers()
      setNotice(
        `扫描完成：新增 ${result.added} · 更新 ${result.updated} · ` +
          `配置里已消失 ${result.missing} · 共 ${result.total}`,
      )
      await refresh()
    } catch (e) {
      setError(describe(e))
    } finally {
      setScanning(false)
    }
  }

  const toggleEnabled = async (server: McpServer) => {
    try {
      await api.updateMcpServer(server.id, { enabled: !server.enabled })
      await refresh()
      if (selected === server.id) setDetail(await api.getMcpServer(server.id))
    } catch (e) {
      setError(describe(e))
    }
  }

  const remove = async (server: McpServer) => {
    if (!confirm(`删除 MCP 服务器「${server.name}」？已分配它的智能体需要先解除分配。`)) return
    try {
      await api.deleteMcpServer(server.id)
      if (selected === server.id) setSelected(null)
      setNotice(`已删除「${server.name}」`)
      await refresh()
    } catch (e) {
      setError(describe(e))
    }
  }

  const groups = useMemo(() => groupBySource(servers), [servers])

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">MCP</h1>
          <p className="page-desc">
            MCP 服务器在这里统一编目：平台新建的「通用」服务器可以分配给任意内核的智能体，
            各内核配置里的服务器只读收录、只能在同内核里分配。未分配的服务器不会被任何智能体加载。
          </p>
        </div>
        <div className="spacer" />
        <input
          className="input"
          style={{ maxWidth: 200 }}
          value={query}
          placeholder="搜索名称、标识或说明"
          onChange={(e) => {
            setQuery(e.target.value)
            void reload({ q: e.target.value, source_kind: sourceKind || undefined })
          }}
        />
        <select
          className="select"
          style={{ maxWidth: 160 }}
          value={sourceKind}
          onChange={(e) => {
            setSourceKind(e.target.value)
            void reload({ source_kind: e.target.value || undefined, q: query || undefined })
          }}
        >
          <option value="">全部来源</option>
          <option value="manual">平台新建</option>
          <option value="user">内核配置</option>
        </select>
        <button className="btn" onClick={() => void rescan()} disabled={scanning}>
          {scanning ? '扫描中…' : '重新扫描'}
        </button>
        <button className="btn primary" onClick={() => setEditing(null)}>
          新建服务器
        </button>
      </header>

      <div className="skill-shell">
        <div className="skill-list">
          {error && <div className="banner err">{error}</div>}
          {notice && <div className="banner ok">{notice}</div>}
          {loading ? (
            <p className="dim small">正在加载…</p>
          ) : servers.length === 0 && !error ? (
            <div className="empty">
              <div className="empty-icon">🔌</div>
              <p>目录里还没有 MCP 服务器。可以点「新建服务器」，或「重新扫描」收录本机已有的配置。</p>
            </div>
          ) : (
            groups.map((group) => (
              <div key={group.key}>
                <div className="card-section-title" style={{ padding: '8px 4px 4px' }}>
                  {group.label}（{group.items.length}）
                </div>
                {group.items.map((server) => (
                  <div
                    key={server.id}
                    className={`chat-item${server.id === selected ? ' active' : ''}`}
                    onClick={() => setSelected(server.id)}
                  >
                    <div className="chat-item-title">{server.name}</div>
                    <div className="chat-item-meta">
                      <span className="pill">{transportLabel(server.transport)}</span>
                      <span className={`pill${server.available ? '' : ' err'}`}>
                        {server.available ? server.source_label : '来源已消失'}
                      </span>
                      {!server.enabled && <span className="pill">已停用</span>}
                      {server.used_by_agents > 0 && (
                        <span className="pill brand">被 {server.used_by_agents} 个智能体使用</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ))
          )}
        </div>

        <div className="skill-pane">
          {detail ? (
            <McpDetailPane
              server={detail}
              onAssign={() => setAssigning(detail)}
              onEdit={() => setEditing(detail)}
              onToggle={() => void toggleEnabled(detail)}
              onDelete={() => void remove(detail)}
            />
          ) : (
            <div className="empty">
              <div className="empty-icon">🔌</div>
              <p>左侧选择一个 MCP 服务器，查看它的连接方式与分配情况。</p>
            </div>
          )}
        </div>
      </div>

      {editing !== undefined && (
        <Modal
          wide
          title={editing ? `编辑 MCP 服务器 · ${editing.name}` : '新建 MCP 服务器'}
          onClose={() => setEditing(undefined)}
        >
          <McpServerForm
            server={editing}
            onCancel={() => setEditing(undefined)}
            onSaved={async (row) => {
              setEditing(undefined)
              setNotice(`已保存「${row.name}」`)
              await refresh()
              setSelected(row.id)
            }}
          />
        </Modal>
      )}

      {assigning && (
        <Modal title={`分配「${assigning.name}」`} onClose={() => setAssigning(null)}>
          <McpAssignPanel
            server={assigning}
            agents={agents}
            onChanged={async () => {
              await refresh()
              setDetail(await api.getMcpServer(assigning.id))
            }}
          />
        </Modal>
      )}
    </>
  )
}

function McpDetailPane({
  server,
  onAssign,
  onEdit,
  onToggle,
  onDelete,
}: {
  server: McpServerDetail
  onAssign: () => void
  onEdit: () => void
  onToggle: () => void
  onDelete: () => void
}) {
  return (
    <div className="stack" style={{ padding: 22, overflow: 'auto' }}>
      <div className="row wrap" style={{ gap: 8 }}>
        <span className="pill brand">{server.source_label}</span>
        {server.type_name && <span className="pill mono-sm">{server.type_name}</span>}
        <span className="pill">{transportLabel(server.transport)}</span>
        <span className="pill mono-sm">{server.slug}</span>
        {!server.available && <span className="pill err">来源已消失</span>}
        {!server.enabled && <span className="pill">已停用</span>}
      </div>

      <div className="row wrap" style={{ gap: 8 }}>
        <button className="btn sm primary" onClick={onAssign}>
          分配给智能体
        </button>
        {server.editable ? (
          <>
            <button className="btn sm" onClick={onEdit}>
              编辑
            </button>
            <button className="btn sm" onClick={onToggle}>
              {server.enabled ? '停用' : '启用'}
            </button>
            <button className="btn sm ghost" onClick={onDelete}>
              删除
            </button>
          </>
        ) : (
          <span className="dim small">内容由该内核自己的配置决定，平台只读收录。</span>
        )}
      </div>

      {server.note && <div className="banner info">{server.note}</div>}

      <div>
        <div className="card-section-title" style={{ marginBottom: 6 }}>
          连接方式
        </div>
        <dl className="kv">
          <dt>传输方式</dt>
          <dd>{transportLabel(server.transport)}</dd>
          {server.transport === 'stdio' ? (
            <>
              <dt>启动命令</dt>
              <dd className="mono-sm">{server.command || '—'}</dd>
              <dt>启动参数</dt>
              <dd className="mono-sm">{server.args.length ? server.args.join(' ') : '—'}</dd>
            </>
          ) : (
            <>
              <dt>服务器地址</dt>
              <dd className="mono-sm">{server.url || '—'}</dd>
            </>
          )}
        </dl>
      </div>

      {(server.env_keys.length > 0 || server.header_keys.length > 0) && (
        <div>
          <div className="card-section-title" style={{ marginBottom: 6 }}>
            密钥（只显示掩码）
          </div>
          <dl className="kv">
            {server.env_keys.map((key) => (
              <div key={`env-${key}`} style={{ display: 'contents' }}>
                <dt className="mono-sm">环境变量 {key}</dt>
                <dd className="mono-sm">{server.env_preview[key] ?? '••••'}</dd>
              </div>
            ))}
            {server.header_keys.map((key) => (
              <div key={`header-${key}`} style={{ display: 'contents' }}>
                <dt className="mono-sm">请求头 {key}</dt>
                <dd className="mono-sm">{server.header_preview[key] ?? '••••'}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      <div>
        <div className="card-section-title" style={{ marginBottom: 6 }}>
          分配给哪些智能体
        </div>
        {server.agents.length === 0 ? (
          <div className="card-sub">尚未分配给任何智能体，当前不会被加载。</div>
        ) : (
          <div className="chips">
            {server.agents.map((agent) => (
              <span className="pill ok" key={agent.id}>
                {agent.avatar} {agent.name}
              </span>
            ))}
          </div>
        )}
      </div>

      <p className="dim small">
        运行时由各内核适配器把它写进该对话的隔离配置：Claude Code 用独立的
        <span className="mono-sm"> --mcp-config </span>并关闭其它 MCP 配置，Codex 只保留分配到的
        <span className="mono-sm"> [mcp_servers] </span>，Gemini CLI 写进工作目录的项目级设置并限定启用范围。
      </p>
    </div>
  )
}

function groupBySource(
  servers: McpServer[],
): { key: string; label: string; items: McpServer[] }[] {
  const groups = new Map<string, { key: string; label: string; items: McpServer[] }>()
  for (const server of servers) {
    const key = server.type_id ?? PLATFORM_GROUP
    const group = groups.get(key) ?? {
      key,
      label: server.type_id ? server.type_name ?? server.type_id : '平台通用（可分配给任意内核）',
      items: [],
    }
    group.items.push(server)
    groups.set(key, group)
  }
  return [...groups.values()]
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
