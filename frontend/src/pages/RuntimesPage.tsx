import { useEffect, useRef, useState } from 'react'
import { RefreshIcon } from '../components/Icons'
import { KernelLogo } from '../components/KernelLogo'
import { Modal } from '../components/Modal'
import { api, ApiError } from '../lib/api'
import { formatListTime } from '../lib/datetime'
import type { AgentRuntime, ProvisionEvent, ProvisionJobInfo, RuntimeAction, RuntimeLatest } from '../lib/types'

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
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const showLogRef = useRef(false)
  const [showLog, setShowLog] = useState(false)
  const [provisionJobs, setProvisionJobs] = useState<Record<string, ProvisionJobInfo>>({})

  const logRef = useRef<HTMLPreElement>(null)

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

  /** 强制重测一个运行时（后端会把结果写回缓存，界面直接用新值覆盖）。 */
  const refreshStatus = async (runtime: AgentRuntime) => {
    setRefreshing((prev) => ({ ...prev, [runtime.id]: true }))
    try {
      const status = await api.refreshRuntime(runtime.id)
      setRuntimes((prev) =>
        prev.map((item) =>
          item.id === runtime.id
            ? {
                ...item,
                provisioning: {
                  ...item.provisioning,
                  installed: status.installed,
                  version: status.version ?? null,
                  path: status.installed ? status.detail ?? null : null,
                  checked_at: status.checked_at ?? null,
                },
              }
            : item,
        ),
      )
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setRefreshing((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  const reload = async () => {
    try {
      const items = await api.listRuntimes()
      setRuntimes(items)
      setError(null)
      // 「检查更新」是每个内核一次远端查询（npm view 之类），不能挡住首屏：
      // 卡片先出来，查到结果再逐个补上，不然页面会一直停在「正在读取运行时状态…」。
      setLoading(false)
      await Promise.all(items.map((item) => checkLatest(item)))
      try {
        const jobs = await api.listProvisionJobs()
        setProvisionJobs(Object.fromEntries(jobs.map((j) => [j.runtime_id, j])))
      } catch {
        /* 忽略 */
      }
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

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight
    }
  }, [job?.lines])

  const start = async (runtime: AgentRuntime, action: RuntimeAction) => {
    if (action === 'uninstall') {
      const command = (runtime.provisioning.commands?.uninstall ?? []).join(' ')
      if (!confirm(`确认卸载运行时「${runtime.name}」？\n将执行：${command}`)) return
    }
    const controller = new AbortController()
    abortRef.current = controller
    setJob({ runtimeId: runtime.id, name: runtime.name, action, lines: [], status: 'running' })
    setShowLog(true)
    showLogRef.current = true
    setProvisionJobs((prev) => ({
      ...prev,
      [runtime.id]: { runtime_id: runtime.id, name: runtime.name, action, status: 'running', started_at: Date.now() / 1000 },
    }))

    const onEvent = (event: ProvisionEvent) => {
      if (event.type === 'provision.started' && event.command) {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: `执行命令：${event.command}` }] } : prev,
        )
      } else if (event.type === 'provision.output') {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: event.text ?? '', level: event.level }] } : prev,
        )
      } else if (event.type === 'provision.finished') {
        setJob((prev) =>
          prev ? { ...prev, status: event.ok ? 'ok' : 'error', error: event.error } : prev,
        )
        setProvisionJobs((prev) =>
          prev[runtime.id]
            ? { ...prev, [runtime.id]: { ...prev[runtime.id], status: event.ok ? 'ok' : 'error', error: event.error } }
            : prev,
        )
        if (!showLogRef.current) {
          setShowLog(true)
          showLogRef.current = true
        }
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
      if (!showLogRef.current) {
        setShowLog(true)
        showLogRef.current = true
      }
    } finally {
      abortRef.current = null
      void reload()
    }
  }

  const cancel = () => {
    abortRef.current?.abort()
    setJob((prev) => (prev ? { ...prev, status: 'error', error: '已中止' } : prev))
  }

  /** 从后端重连到一个进行中 / 已完成的任务，获取全部日志。 */
  const viewProgress = async (runtimeId: string) => {
    const info = provisionJobs[runtimeId]
    if (!info) return
    setShowLog(true)
    showLogRef.current = true
    // 本次会话已启动且有日志，直接展示
    if (job && job.runtimeId === runtimeId && job.lines.length > 0) return
    setJob({ runtimeId, name: info.name, action: info.action, lines: [], status: info.status })
    const controller = new AbortController()
    abortRef.current = controller
    const onEvent = (event: ProvisionEvent) => {
      if (event.type === 'provision.started' && event.command) {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: `执行命令：${event.command}` }] } : prev,
        )
      } else if (event.type === 'provision.output') {
        setJob((prev) =>
          prev ? { ...prev, lines: [...prev.lines, { text: event.text ?? '', level: event.level }] } : prev,
        )
      } else if (event.type === 'provision.finished') {
        setJob((prev) => (prev ? { ...prev, status: event.ok ? 'ok' : 'error', error: event.error } : prev))
        setProvisionJobs((prev) =>
          prev[runtimeId]
            ? { ...prev, [runtimeId]: { ...prev[runtimeId], status: event.ok ? 'ok' : 'error', error: event.error } }
            : prev,
        )
      }
    }
    try {
      await api.streamProvisionJob(runtimeId, onEvent, controller.signal)
    } catch {
      /* 忽略 */
    } finally {
      abortRef.current = null
      void reload()
    }
  }

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
              refreshing={Boolean(refreshing[runtime.id])}
              onRefresh={() => void refreshStatus(runtime)}
              latest={latest[runtime.id]}
              checking={Boolean(checking[runtime.id])}
              busy={provisionJobs[runtime.id]?.status === 'running'}
              runningAction={provisionJobs[runtime.id]?.status === 'running' ? provisionJobs[runtime.id].action : null}
              onViewProgress={() => void viewProgress(runtime.id)}
              onCheck={() => void checkLatest(runtime)}
              onRun={(action) => void start(runtime, action)}
            />
          ))}
        </div>
      </div>

      {job && showLog && (
        <Modal
          title={`${ACTION_LABELS[job.action]}「${job.name}」`}
          onClose={() => {
            if (job.status === 'running') {
              setShowLog(false)
              showLogRef.current = false
            } else {
              if (job) void api.clearProvisionJob(job.runtimeId).catch(() => {})
              setProvisionJobs((prev) => {
                const next = { ...prev }
                if (job) delete next[job.runtimeId]
                return next
              })
              setJob(null)
              setShowLog(false)
              showLogRef.current = false
            }
          }}
          wide
          footer={
            <>
              {job.status === 'running' ? (
                <button className="btn danger" onClick={cancel}>
                  中止
                </button>
              ) : (
                <button
                  className="btn"
                  onClick={() => {
                    if (job) void api.clearProvisionJob(job.runtimeId).catch(() => {})
                    setProvisionJobs((prev) => {
                      const next = { ...prev }
                      if (job) delete next[job.runtimeId]
                      return next
                    })
                    setJob(null)
                    setShowLog(false)
                    showLogRef.current = false
                  }}
                >
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
          <pre className="runtime-log" ref={logRef}>
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
  refreshing: boolean
  busy: boolean
  runningAction: RuntimeAction | null
  onViewProgress: () => void
  onCheck: () => void
  onRefresh: () => void
  onRun: (action: RuntimeAction) => void
}

function RuntimeCard({ runtime, latest, checking, refreshing, busy, runningAction, onViewProgress, onCheck, onRefresh, onRun }: CardProps) {
  const provisioning = runtime.provisioning
  const managed = provisioning.supported
  const remote = latest?.latest ?? null
  const canCheck = Boolean(provisioning.commands?.latest?.length)
  const current = provisioning.version ? versionCore(provisioning.version) : null
  // 查到比本机更高的版本才提示「更新到 X」；已经是最新版就不给更新入口。
  // 查不到（不支持 / 失败 / 还没查）时保留普通「更新」，免得失去升级手段。
  const updateTarget = remote && current && versionCore(remote) !== current ? remote : null
  // 远端最新版本与本机一致时，在版本号旁边挂一个「已是最新」，一眼就知道不用再点更新。
  const upToDate = Boolean(remote && current && versionCore(remote) === current)
  const showUpdate =
    managed &&
    provisioning.installed &&
    Boolean(provisioning.commands?.update?.length) &&
    (updateTarget !== null || !canCheck || !remote)

  return (
    <div className="card runtime-card">
      <div className="runtime-head">
        <KernelLogo logo={runtime.logo} />
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="card-title">{runtime.name}</div>
          <div className="card-sub runtime-sub">
            <span>{kernelSummary(runtime, current)}</span>
            {upToDate && <span className="pill ok">已是最新</span>}
          </div>
          <div className="dim small">{detectedLabel(provisioning.checked_at)}</div>
        </div>
        {/* 「重新检测」收成右上角一个小图标：状态本来就带缓存，这是个偶尔点一次的备用动作 */}
        <button
          className="icon-btn"
          onClick={onRefresh}
          disabled={refreshing || busy}
          aria-label="重新检测"
          title="重新检测安装状态与版本"
        >
          <RefreshIcon size={16} className={refreshing ? 'spin' : undefined} />
        </button>
      </div>

      {managed && provisioning.note && <div className="banner warn">{provisioning.note}</div>}

      <div className="row wrap" style={{ gap: 6, marginTop: 'auto' }}>
        {runningAction && (
          <>
            <span className="pill brand">正在{ACTION_LABELS[runningAction]}…</span>
            <button className="btn sm" onClick={onViewProgress}>
              查看进度
            </button>
          </>
        )}
        {canCheck && (
          <button className="btn sm" onClick={onCheck} disabled={checking || busy}>
            {checking ? '查询中…' : '检查更新'}
          </button>
        )}
        {managed && !provisioning.installed && provisioning.commands?.install?.length ? (
          <button className="btn sm primary" onClick={() => onRun('install')} disabled={busy}>
            安装
          </button>
        ) : null}
        {showUpdate ? (
          <button className="btn sm primary" onClick={() => onRun('update')} disabled={busy}>
            {updateTarget ? `更新到 ${updateTarget}` : '更新'}
          </button>
        ) : null}
        {managed && provisioning.installed && provisioning.commands?.uninstall?.length ? (
          <button className="btn sm danger" onClick={() => onRun('uninstall')} disabled={busy}>
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
}

/** 卡片副标题：所属公司 + 当前版本（卡片上只留这三样，其余信息收进按钮后面的动作里）。 */
function kernelSummary(runtime: AgentRuntime, current: string | null): string {
  const provisioning = runtime.provisioning
  if (!provisioning.supported) {
    return `${runtime.vendor} · ${runtime.runtime === 'api' ? '进程内运行' : '自备可执行文件'}`
  }
  return provisioning.installed && current ? `${runtime.vendor} · 当前版本 ${current}` : `${runtime.vendor} · 未安装`
}

/** 「检测于 …」：安装状态是后端缓存 + 定时检测的，不是每次打开页面实时探的。 */
function detectedLabel(checkedAt: string | null | undefined): string {
  return checkedAt ? `检测于 ${formatListTime(checkedAt)}` : '尚未检测'
}

/** 从版本字符串里抽出可比较的核心版本号（`2.1.316 (Claude Code)` → `2.1.316`）。 */
function versionCore(value: string): string {
  return value.match(/\d+\.\d+(?:\.\d+)?/)?.[0] ?? value.trim()
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
