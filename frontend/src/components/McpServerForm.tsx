import { useState } from 'react'
import { DynamicForm } from './DynamicForm'
import { api, ApiError } from '../lib/api'
import type { ConfigField, McpServer } from '../lib/types'

/** 编辑器里那一组「高级」字段：启动参数 / 环境变量 / 请求头。 */
const ADVANCED_FIELDS: ConfigField[] = [
  {
    key: 'args',
    label: '启动参数',
    type: 'string_list',
    required: false,
    default: null,
    placeholder: '每行一个参数，例如 -y',
    help: 'stdio：传给启动命令的参数。',
    advanced: false,
    options: null,
  },
  {
    key: 'env',
    label: '环境变量',
    type: 'keyvalue',
    required: false,
    default: null,
    placeholder: null,
    help: 'stdio：启动这个服务器时注入的环境变量（值加密保存）。编辑时留空＝保持原值，删掉整行＝删除该变量。',
    advanced: false,
    options: null,
  },
  {
    key: 'headers',
    label: '请求头',
    type: 'keyvalue',
    required: false,
    default: null,
    placeholder: null,
    help: 'http / sse：请求时带的头（例如 Authorization）。同样只回掩码，留空＝保持原值。',
    advanced: false,
    options: null,
  },
]

/**
 * MCP 服务器的编辑表单（两端共用）。
 *
 * 只负责「填 + 存」，弹层外壳交给两端各自实现（桌面端 `Modal`、移动端 `MobileSheet`）：
 * 「留空＝保持原值」这类语义必须只有一份，否则两端很容易长出不一样的实现。
 */
export function McpServerForm({
  server,
  onSaved,
  onCancel,
}: {
  server: McpServer | null
  onSaved: (row: McpServer) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(server?.name ?? '')
  const [slug, setSlug] = useState(server?.slug ?? '')
  const [description, setDescription] = useState(server?.description ?? '')
  const [transport, setTransport] = useState(server?.transport ?? 'stdio')
  const [command, setCommand] = useState(server?.command ?? '')
  const [url, setUrl] = useState(server?.url ?? '')
  const [enabled, setEnabled] = useState(server?.enabled ?? true)
  const [advanced, setAdvanced] = useState<Record<string, unknown>>(() => ({
    args: server?.args ?? [],
    // 已有密钥只回键名，这里以空值占位：留空即保持原值（见接口约定）。
    env: Object.fromEntries((server?.env_keys ?? []).map((key) => [key, ''])),
    headers: Object.fromEntries((server?.header_keys ?? []).map((key) => [key, ''])),
  }))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    if (!name.trim()) return setError('请填写名称')
    setSaving(true)
    setError(null)
    try {
      const payload = {
        name: name.trim(),
        slug: slug.trim() || null,
        description: description.trim() || null,
        transport,
        command: transport === 'stdio' ? command.trim() || null : null,
        url: transport === 'stdio' ? null : url.trim() || null,
        args: advanced.args ?? [],
        env: advanced.env ?? {},
        headers: advanced.headers ?? {},
        enabled,
      }
      const saved = server
        ? await api.updateMcpServer(server.id, payload)
        : await api.createMcpServer(payload)
      onSaved(saved)
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}

      <div className="form-grid">
        <div className="field">
          <label className="field-label">名称</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label className="field-label">标识</label>
          <input
            className="input mono"
            value={slug}
            placeholder="写进内核配置的键名，留空按名称生成"
            onChange={(e) => setSlug(e.target.value)}
          />
          <span className="field-help">同一个智能体挂载的两个服务器不能重名（重名会互相覆盖）。</span>
        </div>
        <div className="field span-2">
          <label className="field-label">说明</label>
          <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div className="field">
          <label className="field-label">传输方式</label>
          <select className="select" value={transport} onChange={(e) => setTransport(e.target.value)}>
            <option value="stdio">本地进程（stdio）</option>
            <option value="http">HTTP</option>
            <option value="sse">SSE</option>
          </select>
        </div>
        {transport === 'stdio' ? (
          <div className="field">
            <label className="field-label">启动命令</label>
            <input
              className="input mono"
              value={command}
              placeholder="例如 npx"
              onChange={(e) => setCommand(e.target.value)}
            />
          </div>
        ) : (
          <div className="field">
            <label className="field-label">服务器地址</label>
            <input
              className="input mono"
              value={url}
              placeholder="https://…"
              onChange={(e) => setUrl(e.target.value)}
            />
          </div>
        )}
        <div className="field span-2">
          <label className="field-label">启用</label>
          <label className="switch">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            <span>{enabled ? '启用（分配它的智能体可以加载）' : '停用（保留配置但不加载）'}</span>
          </label>
        </div>
      </div>

      <DynamicForm fields={ADVANCED_FIELDS} value={advanced} onChange={setAdvanced} />

      <div className="row" style={{ gap: 8, justifyContent: 'flex-end' }}>
        <button className="btn ghost" onClick={onCancel} disabled={saving}>
          取消
        </button>
        <button className="btn primary" onClick={() => void save()} disabled={saving}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
