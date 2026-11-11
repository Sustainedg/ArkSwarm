import { useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api'
import type { AgentRuntime, ProvisionEvent, RuntimeAction, RuntimeLatest } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

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
 * 运行时（移动端）：与桌面端同一套接口，交互改成卡片 + 底部弹层。
 * 安装 / 更新 / 卸载的日志在弹层里滚动，可随时中止。
 */
export function MobileRuntimesPage() {
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
      setLatest((prev) => ({ ...prev, [runtime.id]: { supported: false, latest: null, error: describeError(e) } }))
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
      setError(describeError(e))
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
        setJob((prev) => (prev ? { ...prev, status: event.ok ? 'ok' : 'error', error: event.error } : prev))
      }
    }

    const call =
      action === 'install' ? api.installRuntime : action === 'update' ? api.updateRuntime : api.uninstallRuntime
    try {
      await call(runtime.id, onEvent, controller.signal)
    } catch (e) {
      setJob((prev) =>
        prev ? { ...prev, status: 'error', error: controller.signal.aborted ? '已中止' : describeError(e) } : prev,
      )
    } finally {
      abortRef.current = null
      void reload()
    }
  }

  const instancesInUse = runtimes.reduce((total, runtime) => total + runtime.instances, 0)

  return (
    <div className="m-page">
      <MobileTopBar
        title="运行时"
        subtitle="内核安装状态与版本"
        actions={
          <button className="m-icon-btn" onClick={() => void reload()} aria-label="刷新">
            ↻
          </button>
        }
      />
      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}
          {loading && <div className="m-empty">正在读取运行时状态…</div>}

          <div className="m-list">
            {runtimes.map((runtime) => {
              const provisioning = runtime.provisioning
              const managed = provisioning.supported
              const info = latest[runtime.id]
              const remote = info?.latest ?? null
              const canCheck = Boolean(provisioning.commands?.latest?.length)
              const updateAvailable = Boolean(
                remote && provisioning.version && versionCore(remote) !== versionCore(provisioning.version),
              )
              const busy = job?.status === 'running' && job.runtimeId === runtime.id
              const primaryCommand = provisioning.commands?.update ?? provisioning.commands?.install ?? null
              return (
                <div className="card" key={runtime.id}>
                  <div className="row" style={{ gap: 8 }}>
                    <div className="m-item-main">
                      <div className="m-item-title">{runtime.name}</div>
                      <div className="m-item-sub">
                        {[runtime.vendor, runtime.runtime === 'cli' ? 'CLI' : '进程内 API']
                          .filter(Boolean)
                          .join(' · ')}
                      </div>
                    </div>
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

                  <div className="stack" style={{ gap: 4 }}>
                    {managed && (
                      <div className="row wrap" style={{ gap: 6 }}>
                        <span className="dim small">当前版本</span>
                        <span className="small">
                          {provisioning.version ?? (provisioning.installed ? '未知' : '—')}
                        </span>
                      </div>
                    )}
                    {managed && (
                      <div className="row wrap" style={{ gap: 6 }}>
                        <span className="dim small">最新版本</span>
                        <span className="small">
                          {!canCheck
                            ? '不适用'
                            : checking[runtime.id]
                              ? '查询中…'
                              : remote
                                ? remote
                                : info?.error
                                  ? '查询失败'
                                  : '未检查'}
                        </span>
                      </div>
                    )}
                    {managed && provisioning.path && <div className="mono-sm dim">{provisioning.path}</div>}
                    {managed && provisioning.package && (
                      <div className="dim small">
                        {provisioning.manager} · {provisioning.package}
                      </div>
                    )}
                    <div className="dim small">{runtime.instances} 个智能体实例在用</div>
                    {!managed && (
                      <div className="dim small">
                        {runtime.runtime === 'api' ? '进程内运行，无需安装。' : '由用户自备可执行文件，平台不代管安装。'}
                      </div>
                    )}
                  </div>

                  {managed && primaryCommand && <div className="runtime-cmd">{primaryCommand.join(' ')}</div>}
                  {managed && provisioning.note && <div className="banner warn">{provisioning.note}</div>}

                  <div className="row wrap" style={{ gap: 6 }}>
                    {canCheck && (
                      <button className="btn sm" onClick={() => void checkLatest(runtime)} disabled={Boolean(checking[runtime.id]) || busy}>
                        {checking[runtime.id] ? '查询中…' : '检查更新'}
                      </button>
                    )}
                    {provisioning.supported && !provisioning.installed && provisioning.commands?.install?.length ? (
                      <button className="btn sm primary" onClick={() => void start(runtime, 'install')} disabled={busy}>
                        安装
                      </button>
                    ) : null}
                    {provisioning.supported && provisioning.installed && provisioning.commands?.update?.length ? (
                      <button className="btn sm primary" onClick={() => void start(runtime, 'update')} disabled={busy}>
                        更新
                      </button>
                    ) : null}
                    {provisioning.supported && provisioning.installed && provisioning.commands?.uninstall?.length ? (
                      <button className="btn sm danger" onClick={() => void start(runtime, 'uninstall')} disabled={busy}>
                        卸载
                      </button>
                    ) : null}
                    {runtime.docs_url && (
                      <a className="btn sm ghost" href={runtime.docs_url} target="_blank" rel="noreferrer">
                        文档 ↗
                      </a>
                    )}
                  </div>
                </div>
              )
            })}
          </div>

          {!loading && runtimes.length > 0 && (
            <div className="dim small" style={{ textAlign: 'center' }}>
              共 {runtimes.length} 个运行时 · {instancesInUse} 个智能体实例
            </div>
          )}
        </div>
      </div>

      {job && (
        <MobileSheet
          title={`${ACTION_LABELS[job.action]}「${job.name}」`}
          onClose={() => setJob(null)}
          full
          footer={
            job.status === 'running' ? (
              <button className="btn danger block" onClick={() => abortRef.current?.abort()}>
                中止
              </button>
            ) : (
              <button className="btn block" onClick={() => setJob(null)}>
                关闭
              </button>
            )
          }
        >
          <div className="stack" style={{ gap: 10 }}>
            <div className="row wrap" style={{ gap: 8 }}>
              <span className={`pill ${job.status === 'running' ? 'brand' : job.status === 'ok' ? 'ok' : 'err'}`}>
                {job.status === 'running' ? '进行中…' : job.status === 'ok' ? '完成' : '失败'}
              </span>
              <span className="dim small">任务在服务器本地执行。</span>
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
          </div>
        </MobileSheet>
      )}
    </div>
  )
}

/** 从版本字符串里抽出可比较的核心版本号（`2.1.316 (Claude Code)` → `2.1.316`）。 */
function versionCore(value: string): string {
  return value.match(/\d+\.\d+(?:\.\d+)?/)?.[0] ?? value.trim()
}
