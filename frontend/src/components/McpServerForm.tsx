import { useMemo, useState } from 'react'
import { DynamicForm } from './DynamicForm'
import { api, ApiError } from '../lib/api'
import type { ConfigField, McpServer, McpServerPayload } from '../lib/types'

/**
 * 编辑器里那一组「高级」字段：启动参数 / 环境变量 / 请求头。
 *
 * env / headers 的值不下发到浏览器（接口只回键名 + 掩码），所以把**掩码当占位符**填进
 * 每一行的「值」框：编辑时才分得清「这里空着」还是「已经填好，留空＝保持原值」。
 */
function advancedFields(server: McpServer | null): ConfigField[] {
  return [
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
      help: 'stdio：启动这个服务器时注入的环境变量（值加密保存，只回掩码）。'
        + '编辑时留空＝保持原值（灰字就是已保存值的掩码），删掉整行＝删除该变量。',
      advanced: false,
      options: null,
      entryPlaceholders: server?.env_preview ?? null,
    },
    {
      key: 'headers',
      label: '请求头',
      type: 'keyvalue',
      required: false,
      default: null,
      placeholder: null,
      help: 'http / sse：请求时带的头（例如 Authorization）。同样只回掩码，'
        + '灰字即已保存值；留空＝保持原值，删掉整行＝删除。',
      advanced: false,
      options: null,
      entryPlaceholders: server?.header_preview ?? null,
    },
  ]
}

/**
 * 「表单 / JSON」双视图共用的表单状态：两个视图改的是同一份数据，
 * 切换时互相同步，保存时只有一条提交路径（「留空＝保持原值」的语义不会长出两份）。
 */
interface FormState {
  name: string
  slug: string
  description: string
  transport: string
  command: string
  url: string
  args: string[]
  env: Record<string, string>
  headers: Record<string, string>
  enabled: boolean
}

/** 从服务器记录（或空默认）初始化表单状态。已有密钥只回键名，值以空串占位＝保持原值。 */
function initialState(server: McpServer | null): FormState {
  return {
    name: server?.name ?? '',
    slug: server?.slug ?? '',
    description: server?.description ?? '',
    transport: server?.transport ?? 'stdio',
    command: server?.command ?? '',
    url: server?.url ?? '',
    args: (server?.args as string[] | null) ?? [],
    env: Object.fromEntries((server?.env_keys ?? []).map((key) => [key, ''])),
    headers: Object.fromEntries((server?.header_keys ?? []).map((key) => [key, ''])),
    enabled: server?.enabled ?? true,
  }
}

/** 表单状态 → 提交载荷（空串归一成 null / 删除）。 */
function toPayload(state: FormState): McpServerPayload {
  return {
    name: state.name.trim(),
    slug: state.slug.trim() || null,
    description: state.description.trim() || null,
    transport: state.transport,
    command: state.transport === 'stdio' ? state.command.trim() || null : null,
    url: state.transport === 'stdio' ? null : state.url.trim() || null,
    args: state.args ?? [],
    env: state.env ?? {},
    headers: state.headers ?? {},
    enabled: state.enabled,
  }
}

/** 表单状态 → JSON 视图里编辑的那份文本（字段齐全、顺序固定，密钥值留空＝保持原值）。 */
function toJsonText(state: FormState): string {
  const payload: Record<string, unknown> = {
    name: state.name || '',
    slug: state.slug || '',
    description: state.description || '',
    transport: state.transport,
    ...(state.transport === 'stdio'
      ? { command: state.command || '', args: state.args ?? [] }
      : { url: state.url || '' }),
    env: state.env ?? {},
    headers: state.headers ?? {},
    enabled: state.enabled,
  }
  return JSON.stringify(payload, null, 2)
}

/** JSON 文本 → 表单状态；解析或结构不合法时抛中文错误（消息直接展示给用户）。 */
function fromJsonText(text: string): FormState {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    throw new Error(`JSON 语法不对：${e instanceof Error ? e.message : String(e)}`)
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('JSON 的顶层必须是一个对象 { … }')
  }
  const row = parsed as Record<string, unknown>
  const asString = (value: unknown): string => (typeof value === 'string' ? value : '')
  const asPairs = (value: unknown): Record<string, string> => {
    if (value === undefined || value === null) return {}
    if (typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('「环境变量 / 请求头」必须是「"名字": "值"」的对象')
    }
    const pairs: Record<string, string> = {}
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      if (item === null || item === undefined) continue
      if (typeof item !== 'string' && typeof item !== 'number' && typeof item !== 'boolean') {
        throw new Error(`环境变量 / 请求头的「${key}」值必须是字符串`)
      }
      pairs[key] = String(item)
    }
    return pairs
  }
  const asList = (value: unknown): string[] => {
    if (value === undefined || value === null) return []
    if (typeof value === 'string') return value.split('\n').map((part) => part.trim()).filter(Boolean)
    if (!Array.isArray(value)) throw new Error('「启动参数 args」必须是字符串数组 ["…", "…"]')
    return value.map((item) => String(item))
  }
  const transport = asString(row.transport) || 'stdio'
  if (!['stdio', 'http', 'sse'].includes(transport)) {
    throw new Error(`transport 只能是 stdio / http / sse，收到「${transport}」`)
  }
  return {
    name: asString(row.name).trim(),
    slug: asString(row.slug).trim(),
    description: asString(row.description).trim(),
    transport,
    command: asString(row.command),
    url: asString(row.url),
    args: asList(row.args),
    env: asPairs(row.env),
    headers: asPairs(row.headers),
    enabled: row.enabled === undefined ? true : Boolean(row.enabled),
  }
}

/**
 * JSON 视图的初始文本：编辑时回显当前配置；新建时给一份**带中文说明的完整示例**
 * （含平台自带的「图片生成」服务器，填上网关 / 模型 / 密钥即可直接使用）。
 */
function initialJsonText(state: FormState, server: McpServer | null): string {
  if (server) return toJsonText(state)
  return JSON.stringify(
    {
      _说明: [
        '把下面这些字段改成你的服务器信息，多余的键会被忽略；',
        'transport 是 stdio（本地进程）时填 command + args + env，是 http / sse 时填 url + headers；',
        'env / headers 的值会加密保存，编辑已有服务器时值留空＝保持原值、删掉整行＝删除。',
      ].join('\n'),
      name: '图片生成',
      slug: '',
      description: '调用生图网关 POST /v1/images/generations 生成图片，结果保存到工作目录 generated-images/',
      transport: 'stdio',
      command: 'python3',
      args: ['/path/to/scripts/mcp/image_mcp_server.py'],
      env: {
        IMAGE_GEN_GATEWAY: 'https://api.example.com（生图网关地址，带不带 /v1 都可以）',
        IMAGE_GEN_MODEL: '模型名，例如 flux-dev / gpt-image-1',
        IMAGE_GEN_API_KEY: '网关密钥',
        IMAGE_GEN_TIMEOUT: '生图超时秒数，可不填（默认 300）',
      },
      headers: {},
      enabled: true,
    },
    null,
    2,
  )
}

/**
 * MCP 服务器的编辑表单（两端共用）。
 *
 * 只负责「填 + 存」，弹层外壳交给两端各自实现（桌面端 `Modal`、移动端 `MobileSheet`）：
 * 「留空＝保持原值」这类语义必须只有一份，否则两端很容易长出不一样的实现。
 * 「表单 / JSON」是同一份数据的两个视图：JSON 里「应用到表单」后仍走同一条提交路径。
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
  const [view, setView] = useState<'form' | 'json'>('form')
  const [state, setState] = useState<FormState>(() => initialState(server))
  const [jsonText, setJsonText] = useState<string>(() => initialJsonText(state, server))
  const [jsonError, setJsonError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /** 高级字段（启动参数 / 环境变量 / 请求头）；已有服务器要用它的掩码当占位提示。 */
  const fields = useMemo(() => advancedFields(server), [server])

  /** 内置服务器：参数单独成组，不和「环境变量」键值编辑器重复展示。 */
  const builtinParams = useMemo(() => server?.builtin_params ?? [], [server])
  const builtinKeys = useMemo(
    () => new Set(builtinParams.map((param) => param.key)),
    [builtinParams],
  )
  const isBuiltin = Boolean(server?.builtin_key)
  const extraEnv = useMemo(
    () => Object.fromEntries(Object.entries(state.env).filter(([key]) => !builtinKeys.has(key))),
    [state.env, builtinKeys],
  )

  /** JSON → 表单（切换或保存前调用）；不通过时回 null 并把原因展示出来。 */
  const syncFromJson = (): FormState | null => {
    try {
      const next = fromJsonText(jsonText)
      setJsonError(null)
      setState(next)
      return next
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setJsonError(message)
      return null
    }
  }

  /** 切到 JSON 视图前，把表单里最新的值导出过去（编辑过程中来回切换不丢数据）。 */
  const switchView = (target: 'form' | 'json') => {
    if (target === view) return
    if (target === 'json') {
      setJsonText(toJsonText(state))
      setJsonError(null)
    } else {
      if (!syncFromJson()) return
    }
    setView(target)
  }

  const save = async () => {
    const current = view === 'json' ? syncFromJson() : state
    if (!current) return
    if (!current.name.trim()) return setError('请填写名称')
    // 内置服务器的必填参数：已经存过值（有掩码）时留空＝保持原值，不算缺。
    const missing = builtinParams.filter(
      (param) => param.required && !(current.env[param.key] ?? '').trim() && !param.masked,
    )
    if (missing.length) {
      return setError(`请填写${missing.map((param) => `「${param.label}」`).join('、')}`)
    }
    setSaving(true)
    setError(null)
    try {
      const payload = toPayload(current)
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

  const transportField = useMemo(
    () =>
      state.transport === 'stdio' ? (
        <div className="field">
          <label className="field-label">启动命令</label>
          <input
            className="input mono"
            value={state.command}
            placeholder="例如 npx"
            onChange={(e) => setState({ ...state, command: e.target.value })}
          />
        </div>
      ) : (
        <div className="field">
          <label className="field-label">服务器地址</label>
          <input
            className="input mono"
            value={state.url}
            placeholder="https://…"
            onChange={(e) => setState({ ...state, url: e.target.value })}
          />
        </div>
      ),
    [state],
  )

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}

      <div className="tabs" style={{ alignSelf: 'flex-start' }}>
        <button
          type="button"
          className={`tab${view === 'form' ? ' active' : ''}`}
          onClick={() => switchView('form')}
        >
          表单
        </button>
        <button
          type="button"
          className={`tab${view === 'json' ? ' active' : ''}`}
          onClick={() => switchView('json')}
        >
          JSON
        </button>
      </div>

      {view === 'form' ? (
        <>
          {isBuiltin && (
            <div className="banner info">
              这是平台内置的 MCP 服务器：启动命令与脚本由平台维护，填好下面的参数即可使用。
            </div>
          )}

          <div className="form-grid">
            <div className="field">
              <label className="field-label">名称</label>
              <input
                className="input"
                value={state.name}
                onChange={(e) => setState({ ...state, name: e.target.value })}
              />
            </div>
            {!isBuiltin && (
              <div className="field">
                <label className="field-label">标识</label>
                <input
                  className="input mono"
                  value={state.slug}
                  placeholder="写进内核配置的键名，留空按名称生成"
                  onChange={(e) => setState({ ...state, slug: e.target.value })}
                />
                <span className="field-help">同一个智能体挂载的两个服务器不能重名（重名会互相覆盖）。</span>
              </div>
            )}
            <div className="field span-2">
              <label className="field-label">说明</label>
              <input
                className="input"
                value={state.description}
                onChange={(e) => setState({ ...state, description: e.target.value })}
              />
            </div>
            {isBuiltin ? (
              <div className="field span-2">
                <label className="field-label">连接方式</label>
                <span className="field-help">
                  平台内置维护：{server?.command} {server?.args.join(' ')}
                </span>
              </div>
            ) : (
              <>
                <div className="field">
                  <label className="field-label">传输方式</label>
                  <select
                    className="select"
                    value={state.transport}
                    onChange={(e) => setState({ ...state, transport: e.target.value })}
                  >
                    <option value="stdio">本地进程（stdio）</option>
                    <option value="http">HTTP</option>
                    <option value="sse">SSE</option>
                  </select>
                </div>
                {transportField}
              </>
            )}
            <div className="field span-2">
              <label className="field-label">启用</label>
              <label className="switch">
                <input
                  type="checkbox"
                  checked={state.enabled}
                  onChange={(e) => setState({ ...state, enabled: e.target.checked })}
                />
                <span>{state.enabled ? '启用（分配它的智能体可以加载）' : '停用（保留配置但不加载）'}</span>
              </label>
            </div>
          </div>

          {builtinParams.length > 0 && (
            <div className="form-grid">
              {builtinParams.map((param) => (
                <div className="field" key={param.key}>
                  <label className="field-label">
                    {param.label}
                    {param.required && <span className="pill err">必填</span>}
                  </label>
                  <input
                    className="input mono"
                    type={param.secret ? 'password' : 'text'}
                    value={state.env[param.key] ?? ''}
                    placeholder={param.masked ?? param.placeholder}
                    onChange={(e) =>
                      setState({ ...state, env: { ...state.env, [param.key]: e.target.value } })
                    }
                  />
                  {param.help && <span className="field-help">{param.help}</span>}
                </div>
              ))}
            </div>
          )}

          <DynamicForm
            fields={fields}
            value={{ args: state.args, env: extraEnv, headers: state.headers }}
            onChange={(next) => {
              const nextEnv = (next.env as Record<string, string>) ?? {}
              const kept = Object.fromEntries(
                Object.entries(state.env).filter(([key]) => builtinKeys.has(key)),
              )
              setState({
                ...state,
                args: (next.args as string[]) ?? [],
                env: { ...kept, ...nextEnv },
                headers: (next.headers as Record<string, string>) ?? {},
              })
            }}
          />
        </>
      ) : (
        <div className="field">
          <label className="field-label">
            服务器配置（JSON）
            {server && (
              <span className="field-help" style={{ display: 'block' }}>
                已有的密钥只回键名：值留空＝保持原值，删掉整行＝删除。
              </span>
            )}
          </label>
          <textarea
            className="textarea mono"
            spellCheck={false}
            rows={Math.max(14, Math.min(32, jsonText.split('\n').length + 2))}
            value={jsonText}
            onChange={(e) => {
              setJsonText(e.target.value)
              setJsonError(null)
            }}
          />
          {jsonError && <div className="banner err">{jsonError}</div>}
          {view === 'json' && (
            <span className="field-help">
              改完直接点「保存」即可（会先校验 JSON）；也可以切回「表单」视图继续编辑。
            </span>
          )}
        </div>
      )}

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
