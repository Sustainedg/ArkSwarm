import { useEffect, useRef, useState } from 'react'
import { Modal } from '../components/Modal'
import { api, ApiError } from '../lib/api'
import type { AgentRuntime, ProvisionEvent, RuntimeAction, RuntimeLatest } from '../lib/types'

const ACTION_LABELS: Record<RuntimeAction, string> = {
  install: '安装',
  update: '更新',
  uninstall: '卸载',
}

interface LogLine {
  text: string
  level?: string
}

interface Job {
  runtimeId: string
  name: string
  action: RuntimeAction
  lines: LogLine[]
  status: 'running' | 'ok' | 'error'
  error?: string | null
}

/**
 * 运行时页：管理各 CLI 内核在本机的那份可执行文件。
 *
 * 与「智能体配置」不同：那里配置的是「怎么调用某个内核」，这里管的是「内核本身装没装、
 * 什么版本、要不要升级」。安装 / 更新 / 卸载都是改本机环境的长任务，因此执行过程
 * 以日志流实时回显，并且随时可以中止（中止即杀掉子进程）。
 */
export function RuntimesPage() {
  const [runtimes, setRuntimes] = useState<AgentRuntime[]>([])
  const [latest, setLatest] = useState<Record<string, RuntimeLatest>>({})
  const [checking, setChecking] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const checkLatest = async (runtime: AgentRuntime) => {
    if (!runtime.provisioning.commands?.latest?.length) return
    setChecking((prev) => ({ ...prev, [runtime.id]: true }))
    try {
      const result = await api.checkRuntimeLatest(runtime.id)
      setLatest((prev) => ({ ...prev, [runtime.id]: result }))
    } catch (e) {
      setLatest((prev) => ({ ...prev, [runtime.id]: { supported: false, latest: null, error: describe(e) } }))
    } finally {
      setChecking((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  const reload = async () => {
    try {
      const items = await api.listRuntimes()
      setRuntimes(items)
      setError(null)
      await Promise.all(items.map((item) => checkLatest(item)))
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
    return () => abortRef.current?.abort()
  }, [])

  const start = async (runtime: AgentRuntime, action: RuntimeAction) => {
    if (action === 'uninstall') {
      const command = (runtime.provisioning.commands?.uninstall ?? []).join(' ')
      if (!confirm(`确认卸载运行时「${runtime.name}」？\n将执行：${command}`)) return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setJob({ runtimeId: runtime.id, name: runtime.name, action, lines: [], status: 'running' })

    const onEvent = (event: ProvisionEvent) => {
      if (event.type === 'provision.output') {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: event.text ?? '', level: event.level }] } : prev,
        )
      } else if (event.type === 'provision.finished') {
        setJob((prev) =>
          prev ? { ...prev, status: event.ok ? 'ok' : 'error', error: event.error } : prev,
        )
      }
    }

    const call =
      action === 'install' ? api.installRuntime : action === 'update' ? api.updateRuntime : api.uninstallRuntime
    try {
      await call(runtime.id, onEvent, controller.signal)
    } catch (e) {
      setJob((prev) =>
        prev ? { ...prev, status: 'error', error: controller.signal.aborted ? '已中止' : describe(e) } : prev,
      )
    } finally {
      abortRef.current = null
      void reload()
    }
  }

  const cancel = () => {
    abortRef.current?.abort()
    setJob((prev) => (prev ? { ...prev, status: 'error', error: '已中止' } : prev))
  }

  const running = job?.status === 'running'

  return (
    <>
      <header className="page-header">
        <div>
          <h1 className="page-title">运行时</h1>
          <p className="page-desc">
            各 CLI 内核在本机的那份可执行文件。可以查看安装状态与版本，并直接安装、更新或卸载。
          </p>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={() => void reload()} disabled={loading}>
          ↻ 刷新
        </button>
      </header>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}
        {loading && <div className="empty"><p>正在读取运行时状态…</p></div>}
        {!loading && runtimes.length === 0 && !error && (
          <div className="empty">
            <div className="empty-icon">🧬</div>
            <p>没有已注册的运行时。</p>
          </div>
        )}

        <div className="runtime-grid">
          {runtimes.map((runtime) => (
            <RuntimeCard
              key={runtime.id}
              runtime={runtime}
              latest={latest[runtime.id]}
              checking={Boolean(checking[runtime.id])}
              busy={running && job?.runtimeId === runtime.id}
              onCheck={() => void checkLatest(runtime)}
              onRun={(action) => void start(runtime, action)}
            />
          ))}
        </div>
      </div>

      {job && (
        <Modal
          title={`${ACTION_LABELS[job.action]}「${job.name}」`}
          onClose={() => setJob(null)}
          wide
          footer={
            <>
              {job.status === 'running' ? (
                <button className="btn danger" onClick={cancel}>
                  中止
                </button>
              ) : (
                <button className="btn" onClick={() => setJob(null)}>
                  关闭
                </button>
              )}
            </>
          }
        >
          <div className="row wrap" style={{ gap: 8 }}>
            <span className={`pill ${job.status === 'running' ? 'brand' : job.status === 'ok' ? 'ok' : 'err'}`}>
              {job.status === 'running' ? '进行中…' : job.status === 'ok' ? '完成' : '失败'}
            </span>
            <span className="dim small">{ACTION_LABELS[job.action]}任务在服务器本地执行，关掉窗口不会中断。</span>
          </div>
          {job.error && <div className={`banner ${job.status === 'ok' ? 'info' : 'err'}`}>{job.error}</div>}
          <pre className="runtime-log">
            {job.lines.length === 0
              ? '等待输出…'
              : job.lines.map((line, index) => (
                  <span key={index} className={line.level === 'warn' ? 'runtime-log-warn' : undefined}>
                    {line.text}
                    {'\n'}
                  </span>
                ))}
          </pre>
        </Modal>
      )}
    </>
  )
}

interface CardProps {
  runtime: AgentRuntime
  latest?: RuntimeLatest
  checking: boolean
  busy: boolean
  onCheck: () => void
  onRun: (action: RuntimeAction) => void
}

function RuntimeCard({ runtime, latest, checking, busy, onCheck, onRun }: CardProps) {
  const provisioning = runtime.provisioning
  const managed = provisioning.supported
  const remote = latest?.latest ?? null
  const canCheck = Boolean(provisioning.commands?.latest?.length)
  const updateAvailable = Boolean(
    remote && provisioning.version && versionCore(remote) !== versionCore(provisioning.version),
  )
  const primaryCommand =
    provisioning.commands?.update ?? provisioning.commands?.install ?? null

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <div className="card-title">{runtime.name}</div>
          <div className="card-sub">
            {[runtime.vendor, runtime.runtime === 'cli' ? 'CLI 子进程' : '进程内 API'].filter(Boolean).join(' · ')}
          </div>
        </div>
        <div className="card-actions">
          {managed ? (
            provisioning.installed ? (
              <span className="pill ok">已安装</span>
            ) : (
              <span className="pill warn">未安装</span>
            )
          ) : (
            <span className="pill">{runtime.runtime === 'api' ? '进程内' : '自备'}</span>
          )}
          {updateAvailable && <span className="pill brand">有更新</span>}
        </div>
      </div>

      <p className="dim small" style={{ margin: 0 }}>
        {runtime.description}
      </p>

      <dl className="kv">
        {managed && (
          <>
            <dt>当前版本</dt>
            <dd>{provisioning.version ?? (provisioning.installed ? '未知' : '—')}</dd>
            <dt>最新版本</dt>
            <dd>
              {!canCheck ? (
                <span className="dim">不适用</span>
              ) : checking ? (
                <span className="dim">查询中…</span>
              ) : remote ? (
                remote
              ) : (
                <span className="dim">{latest?.error ? '查询失败' : '未检查'}</span>
              )}
            </dd>
            <dt>安装位置</dt>
            <dd className="mono-sm">{provisioning.path ?? (provisioning.installed ? '—' : '未检测到')}</dd>
          </>
        )}
        <dt>使用中的实例</dt>
        <dd>{runtime.instances} 个智能体</dd>
        {managed && (
          <>
            <dt>安装方式</dt>
            <dd>{[provisioning.manager, provisioning.package].filter(Boolean).join(' · ')}</dd>
          </>
        )}
      </dl>

      {managed && primaryCommand && <div className="runtime-cmd">{primaryCommand.join(' ')}</div>}
      {managed && provisioning.note && <div className="banner warn">{provisioning.note}</div>}
      {!managed && (
        <div className="dim small">
          {runtime.runtime === 'api' ? '进程内运行，无需安装。' : '由用户自备可执行文件，平台不代管安装。'}
        </div>
      )}

      <div className="row wrap" style={{ gap: 6, marginTop: 'auto' }}>
        {canCheck && (
          <button className="btn sm" onClick={onCheck} disabled={checking || busy}>
            {checking ? '查询中…' : '检查更新'}
          </button>
        )}
        {managed && (
          <>
            {!provisioning.installed && provisioning.commands?.install?.length ? (
              <button className="btn sm primary" onClick={() => onRun('install')} disabled={busy}>
                安装
              </button>
            ) : null}
            {provisioning.installed && provisioning.commands?.update?.length ? (
              <button className="btn sm primary" onClick={() => onRun('update')} disabled={busy}>
                更新
              </button>
            ) : null}
            {provisioning.installed && provisioning.commands?.uninstall?.length ? (
              <button className="btn sm danger" onClick={() => onRun('uninstall')} disabled={busy}>
                卸载
              </button>
            ) : null}
          </>
        )}
        {runtime.docs_url && (
          <a className="btn sm ghost" href={runtime.docs_url} target="_blank" rel="noreferrer">
            文档 ↗
          </a>
        )}
      </div>
    </div>
  )
}

/** 从版本字符串里抽出可比较的核心版本号（`2.1.316 (Claude Code)` → `2.1.316`）。 */
function versionCore(value: string): string {
  return value.match(/\d+\.\d+(?:\.\d+)?/)?.[0] ?? value.trim()
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
