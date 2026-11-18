import { useCallback, useEffect, useState } from 'react'
import { McpAssignPanel } from '../../components/McpAssignPanel'
import { McpDetailBody } from '../../components/McpDetailBody'
import { McpServerForm } from '../../components/McpServerForm'
import { transportLabel } from '../../components/McpPicker'
import { api } from '../../lib/api'
import { hasMissingBuiltinParams } from '../../lib/mcp'
import type { AgentProfile, McpServer, McpServerDetail } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/** MCP：统一编目 + 通过智能体分配生效（桌面端 MCP 页的移动版）。 */
export function MobileMcpPage() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [query, setQuery] = useState('')
  const [sourceKind, setSourceKind] = useState('')
  const [detailId, setDetailId] = useState<string | null>(null)
  const [detail, setDetail] = useState<McpServerDetail | null>(null)
  const [assigning, setAssigning] = useState(false)
  // undefined＝关闭；null＝新建；对象＝编辑
  const [editing, setEditing] = useState<McpServer | null | undefined>(undefined)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [scanning, setScanning] = useState(false)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(
    async (params?: { source_kind?: string; q?: string }) => {
      try {
        const [list, agentList] = await Promise.all([
          api.listMcpServers(params),
          api.listAgents(),
        ])
        setServers(list)
        setAgents(agentList)
        setError(null)
      } catch (e) {
        setError(describeError(e))
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    void reload()
  }, [reload])

  const refresh = () => reload({ source_kind: sourceKind || undefined, q: query || undefined })

  const openDetail = async (server: McpServer) => {
    setDetailId(server.id)
    setDetail(null)
    try {
      setDetail(await api.getMcpServer(server.id))
    } catch (e) {
      setError(describeError(e))
    }
  }

  const reloadDetail = async (id: string) => {
    await refresh()
    try {
      setDetail(await api.getMcpServer(id))
    } catch {
      setDetail(null)
    }
  }

  const rescan = async () => {
    setScanning(true)
    try {
      const result = await api.rescanMcpServers()
      setNotice(
        `扫描完成：新增 ${result.added} · 更新 ${result.updated} · 配置里已消失 ${result.missing}`,
      )
      await refresh()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setScanning(false)
    }
  }

  const toggleEnabled = async (server: McpServerDetail) => {
    try {
      await api.updateMcpServer(server.id, { enabled: !server.enabled })
      await reloadDetail(server.id)
    } catch (e) {
      setError(describeError(e))
    }
  }

  const remove = async (server: McpServerDetail) => {
    if (!confirm(`删除 MCP 服务器「${server.name}」？已分配它的智能体需要先解除分配。`)) return
    try {
      await api.deleteMcpServer(server.id)
      setDetailId(null)
      setDetail(null)
      setNotice(`已删除「${server.name}」`)
      await refresh()
    } catch (e) {
      setError(describeError(e))
    }
  }

  const grouped = groupBySource(servers)

  return (
    <div className="m-page">
      <MobileTopBar
        title="MCP"
        subtitle="统一编目 · 通过智能体分配生效"
        actions={
          <button className="btn sm primary" onClick={() => setEditing(null)}>
            新建
          </button>
        }
      />
      <div className="m-page-body">
        <div className="stack">
          <div className="row" style={{ gap: 8 }}>
            <span className="dim small" style={{ flex: 1 }}>
              共 {servers.length} 个服务器
            </span>
            <button className="btn" onClick={() => void rescan()} disabled={scanning}>
              {scanning ? '扫描中…' : '重新扫描'}
            </button>
          </div>
          <input
            className="input"
            value={query}
            placeholder="搜索名称、标识或说明"
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
            <option value="">全部来源</option>
            <option value="manual">平台新建</option>
            <option value="user">内核配置</option>
          </select>

          {error && <div className="banner err">{error}</div>}
          {notice && <div className="banner info">{notice}</div>}

          {loading ? (
            <p className="dim small">正在加载…</p>
          ) : servers.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">🔌</div>
              <p>目录里还没有 MCP 服务器</p>
              <p className="dim small">点右上角「新建」添加，或「重新扫描」收录本机已有的配置。</p>
            </div>
          ) : (
            grouped.map((group) => (
              <div className="stack" key={group.key} style={{ gap: 8 }}>
                <div className="card-section-title">
                  {group.label}（{group.items.length}）
                </div>
                {group.items.map((server) => (
                  <button key={server.id} className="m-item" onClick={() => void openDetail(server)}>
                    <span className="m-item-main">
                      <span className="m-item-title">
                        {server.name}
                        {!server.enabled && <span className="pill">已停用</span>}
                      </span>
                      <span className="m-item-sub">
                        {transportLabel(server.transport)} ·{' '}
                        {server.description || server.url || server.command || '（无描述）'}
                      </span>
                    </span>
                    <span className={`pill${server.available ? '' : ' err'}`}>
                      {server.available
                        ? hasMissingBuiltinParams(server)
                          ? '待填参数'
                          : server.used_by_agents > 0
                            ? `${server.used_by_agents} 个智能体`
                            : server.source_label
                        : '来源已消失'}
                    </span>
                    <span className="m-item-chevron">›</span>
                  </button>
                ))}
              </div>
            ))
          )}
        </div>
      </div>

      {detailId && detail && (
        <MobileSheet
          full
          title={`MCP · ${detail.name}`}
          onClose={() => {
            setDetailId(null)
            setDetail(null)
          }}
        >
          <McpDetailBody
            server={detail}
            showName={false}
            onAssign={() => setAssigning(true)}
            onEdit={() => setEditing(detail)}
            onToggle={() => void toggleEnabled(detail)}
            onDelete={() => void remove(detail)}
          />
        </MobileSheet>
      )}

      {assigning && detail && (
        <MobileSheet full title={`分配「${detail.name}」`} onClose={() => setAssigning(false)}>
          <McpAssignPanel
            server={detail}
            agents={agents}
            onChanged={() => reloadDetail(detail.id)}
          />
        </MobileSheet>
      )}

      {editing !== undefined && (
        <MobileSheet
          full
          title={editing ? `编辑 MCP · ${editing.name}` : '新建 MCP 服务器'}
          onClose={() => setEditing(undefined)}
        >
          <McpServerForm
            server={editing}
            onCancel={() => setEditing(undefined)}
            onSaved={async (row) => {
              setEditing(undefined)
              setNotice(`已保存「${row.name}」`)
              await refresh()
              await openDetail(row)
            }}
          />
        </MobileSheet>
      )}
    </div>
  )
}

function groupBySource(
  servers: McpServer[],
): { key: string; label: string; items: McpServer[] }[] {
  const groups = new Map<string, { key: string; label: string; items: McpServer[] }>()
  for (const server of servers) {
    const key = server.type_id ?? '__platform__'
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
