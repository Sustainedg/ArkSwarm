import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { AgentType, Overview } from '../lib/types'

export function SettingsPage() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [types, setTypes] = useState<AgentType[]>([])
  const [error, setError] = useState<string | null>(null)

  const reload = async () => {
    try {
      const [info, typeList] = await Promise.all([api.overview(), api.agentTypes()])
      setOverview(info)
      setTypes(typeList)
    } catch (e) {
      setError(describe(e))
    }
  }

  useEffect(() => {
    void reload()
    const timer = setInterval(() => void reload(), 8000)
    return () => clearInterval(timer)
  }, [])

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">系统</h1>
          <p className="page-desc">运行时状态、已注册的适配器与扩展点说明。</p>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={() => void reload()}>
          ↻ 刷新
        </button>
      </header>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}

        {overview && (
          <div className="grid">
            <Stat label="版本" value={overview.version} />
            <Stat label="适配器" value={`${overview.installed_types} / ${overview.agent_types} 可用`} />
            <Stat label="数据目录" value={overview.data_dir} mono />
            <Stat label="工作目录根" value={overview.workspaces_root} mono />
          </div>
        )}

        <section className="stack">
          <h2 className="card-section-title">正在运行的 Agent</h2>
          {overview && overview.active_runs.length > 0 ? (
            <div className="grid">
              {overview.active_runs.map((run) => (
                <div className="card" key={run.id}>
                  <div className="card-head">
                    <div>
                      <div className="card-title">{run.agent_name}</div>
                      <div className="card-sub mono-sm">{run.id}</div>
                    </div>
                    <span className="pill ok" style={{ marginLeft: 'auto' }}>
                      <span className="dot" />
                      {run.status} · {run.elapsed_seconds}s
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="dim small">当前没有运行中的任务。</p>
          )}
        </section>

        <section className="stack">
          <h2 className="card-section-title">已注册的适配器</h2>
          <div className="grid">
            {types.map((type) => (
              <article className="card" key={type.id}>
                <div className="card-head">
                  <div>
                    <div className="card-title">{type.name}</div>
                    <div className="card-sub">
                      {type.vendor} · <span className="mono-sm">{type.id}</span>
                    </div>
                  </div>
                  <span className={`pill ${type.runtime === 'api' ? 'brand' : ''}`} style={{ marginLeft: 'auto' }}>
                    {type.runtime === 'api' ? 'API 进程内' : 'CLI 子进程'}
                  </span>
                </div>
                <p className="small muted" style={{ lineHeight: 1.55 }}>
                  {type.description}
                </p>
                <div className="chips">
                  {type.availability.installed ? (
                    <span className="pill ok">{type.availability.version ?? '就绪'}</span>
                  ) : (
                    <span className="pill err" title={type.availability.detail ?? ''}>
                      未安装：{type.executable}
                    </span>
                  )}
                  {type.capabilities.sessions && <span className="pill">多轮会话</span>}
                  {type.capabilities.tools && <span className="pill">工具调用</span>}
                  {type.capabilities.mcp && <span className="pill">MCP</span>}
                  {type.capabilities.native_system_prompt && <span className="pill">原生人格</span>}
                </div>
                <dl className="kv">
                  <dt>默认网关</dt>
                  <dd title={type.default_gateway ?? ''}>{type.default_gateway ?? '—'}</dd>
                  <dt>默认模型</dt>
                  <dd>{type.default_model ?? '—'}</dd>
                  <dt>凭据变量</dt>
                  <dd className="mono-sm">{type.api_key_env ?? '—'}</dd>
                  <dt>网关变量</dt>
                  <dd className="mono-sm">{type.base_url_env ?? '—'}</dd>
                </dl>
                {type.docs_url && (
                  <a className="small" href={type.docs_url} target="_blank" rel="noreferrer">
                    查看文档 ↗
                  </a>
                )}
              </article>
            ))}
          </div>
        </section>

        <section className="stack">
          <h2 className="card-section-title">如何接入一个新的 Agent</h2>
          <div className="card">
            <ol style={{ paddingLeft: 18, lineHeight: 1.9, fontSize: 13 }} className="muted">
              <li>
                在 <code className="mono-sm">backend/app/agents/builtin/</code> 新增一个模块，继承{' '}
                <code className="mono-sm">AgentDefinition</code>，用 <code className="mono-sm">@register_agent</code> 装饰。
              </li>
              <li>
                CLI 型：实现 <code className="mono-sm">build_invocation()</code> 与{' '}
                <code className="mono-sm">parse_line()</code>；API 型：实现{' '}
                <code className="mono-sm">async stream()</code>。
              </li>
              <li>用 config_fields 声明可配置项 —— 前端表单会自动生成，无需改前端。</li>
              <li>
                想完全不写代码？直接用「Custom CLI」适配器：填命令、参数模板与 JSON 取值路径即可。
              </li>
            </ol>
          </div>
        </section>
      </div>
    </>
  )
}

function Stat({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="card" style={{ gap: 6 }}>
      <span className="card-section-title">{label}</span>
      <span className={mono ? 'mono-sm' : ''} style={{ wordBreak: 'break-all' }}>
        {value}
      </span>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
