import { useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api'
import type { AgentRuntime, ProvisionEvent, RuntimeAction, RuntimeLatest } from '../../lib/types'
import { RefreshIcon } from '../../components/Icons'
import { KernelLogo } from '../../components/KernelLogo'
import { MobileAgentSettings } from '../components/MobileAgentSettings'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError, relativeTime } from '../util'

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
 *
 * 一个内核 = 一份安装 + N 份智能体配置，所以「装 / 升 / 卸」与「这个内核怎么跑」
 * 都在这里：卡片里的「设置」直接编辑该内核下的智能体实例（网关 · 密钥 · 模型 · 适配器选项）。
 * 日志在弹层里滚动，可随时中止。
 */
export function MobileRuntimesPage() {
  const [runtimes, setRuntimes] = useState<AgentRuntime[]>([])
  const [latest, setLatest] = useState<Record<string, RuntimeLatest>>({})
  const [checking, setChecking] = useState<Record<string, boolean>>({})
  const [refreshing, setRefreshing] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  /** 正在编辑设置的内核（适配器类型 id）：非空时弹出该内核的智能体设置 */
  const [settingsFor, setSettingsFor] = useState<AgentRuntime | null>(null)
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

  /** 强制重测一个运行时（后端把结果写回缓存，界面直接用新值覆盖）。 */
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
      setError(describeError(e))
    } finally {
      setRefreshing((prev) => ({ ...prev, [runtime.id]: false }))
    }
  }

  const reload = async () => {
    try {
      const items = await api.listRuntimes()
      setRuntimes(items)
      setError(null)
      // 「检查更新」是每个内核一次远端查询，不能挡住首屏：卡片先出来，结果到了再补。
      setLoading(false)
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
              const remote = latest[runtime.id]?.latest ?? null
              const canCheck = Boolean(provisioning.commands?.latest?.length)
              const current = provisioning.version ? versionCore(provisioning.version) : null
              // 查到更高版本才提示「更新到 X」；已经是最新版就不给更新入口。
              // 查不到（不支持 / 失败 / 还没查）时保留普通「更新」，免得失去升级手段。
              const updateTarget = remote && current && versionCore(remote) !== current ? remote : null
              // 远端最新版本与本机一致时，在版本号旁边挂一个「已是最新」。
              const upToDate = Boolean(remote && current && versionCore(remote) === current)
              const showUpdate =
                managed &&
                provisioning.installed &&
                Boolean(provisioning.commands?.update?.length) &&
                (updateTarget !== null || !canCheck || !remote)
              const busy = job?.status === 'running' && job.runtimeId === runtime.id
              return (
                <div className="card runtime-card" key={runtime.id}>
                  <div className="runtime-head">
                    <KernelLogo logo={runtime.logo} />
                    <div className="m-item-main">
                      <div className="m-item-title">{runtime.name}</div>
                      <div className="m-item-sub runtime-sub">
                        <span>{kernelSummary(runtime, current)}</span>
                        {upToDate && <span className="pill ok">已是最新</span>}
                      </div>
                      <div className="dim small">{detectedLabel(provisioning.checked_at)}</div>
                    </div>
                    {/* 「重新检测」收成右上角一个小图标（状态本来就带缓存，偶尔点一次） */}
                    <button
                      className="m-icon-btn"
                      onClick={() => void refreshStatus(runtime)}
                      disabled={Boolean(refreshing[runtime.id]) || busy}
                      aria-label="重新检测"
                      title="重新检测安装状态与版本"
                    >
                      <RefreshIcon size={17} className={refreshing[runtime.id] ? 'm-spin' : undefined} />
                    </button>
                  </div>

                  {managed && provisioning.note && <div className="banner warn">{provisioning.note}</div>}

                  <div className="row wrap" style={{ gap: 6 }}>
                    {canCheck && (
                      <button
                        className="btn sm"
                        onClick={() => void checkLatest(runtime)}
                        disabled={Boolean(checking[runtime.id]) || busy}
                      >
                        {checking[runtime.id] ? '查询中…' : '检查更新'}
                      </button>
                    )}
                    {managed && !provisioning.installed && provisioning.commands?.install?.length ? (
                      <button className="btn sm primary" onClick={() => void start(runtime, 'install')} disabled={busy}>
                        安装
                      </button>
                    ) : null}
                    {showUpdate ? (
                      <button className="btn sm primary" onClick={() => void start(runtime, 'update')} disabled={busy}>
                        {updateTarget ? `更新到 ${updateTarget}` : '更新'}
                      </button>
                    ) : null}
                    {managed && provisioning.installed && provisioning.commands?.uninstall?.length ? (
                      <button className="btn sm danger" onClick={() => void start(runtime, 'uninstall')} disabled={busy}>
                        卸载
                      </button>
                    ) : null}
                    <button className="btn sm primary" onClick={() => setSettingsFor(runtime)} disabled={busy}>
                      设置
                    </button>
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
        </div>
      </div>

      {settingsFor && (
        <MobileSheet
          full
          title={`${settingsFor.name} · 设置`}
          onClose={() => {
            setSettingsFor(null)
            void reload()
          }}
        >
          <MobileAgentSettings typeId={settingsFor.id} />
        </MobileSheet>
      )}

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

/** 卡片副标题：所属公司 + 当前版本（卡片上只留这三样，其余信息收进按钮后面的动作里）。 */
function kernelSummary(runtime: AgentRuntime, current: string | null): string {
  const provisioning = runtime.provisioning
  if (!provisioning.supported) {
    return `${runtime.vendor} · ${runtime.runtime === 'api' ? '进程内运行' : '自备可执行文件'}`
  }
  return provisioning.installed && current ? `${runtime.vendor} · 当前版本 ${current}` : `${runtime.vendor} · 未安装`
}

/** 「检测于 …」：安装状态由后端缓存 + 定时检测，不是每次打开页面实时探的。 */
function detectedLabel(checkedAt: string | null | undefined): string {
  const text = relativeTime(checkedAt)
  return text ? `检测于 ${text}` : '尚未检测'
}

/** 从版本字符串里抽出可比较的核心版本号（`2.1.316 (Claude Code)` → `2.1.316`）。 */
function versionCore(value: string): string {
  return value.match(/\d+\.\d+(?:\.\d+)?/)?.[0] ?? value.trim()
}
