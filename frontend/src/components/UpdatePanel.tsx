import { useEffect, useRef, useState } from 'react'
import { api, ApiError, updateStream } from '../lib/api'
import { isNetworkError } from '../lib/api'
import type { UpdateStatus } from '../lib/types'

/**
 * 「设置 → 关于」的版本更新面板：检测更新（GitHub Release）→ 一键更新 / 回滚。
 *
 * 流程（安装 / 回滚是 SSE 长动作，最后进程会原地重启）：
 *   1. `POST /api/update/install` 逐条收 `update.*` 事件；
 *   2. 收到 `update.restarting` 后进入「等待服务回来」——轮询 `/api/health`，
 *      exec 重启有几秒不可用窗口，接口 200 且版本号变化即成功；
 *   3. 刷新整页（`location.reload()`）：前端产物也换新了，必须重载才拿到。
 *
 * 源码模式（`mode === 'source'`）只提示「有新版本，请用 git 更新」，不给按钮。
 */
export function UpdatePanel({ currentVersion }: { currentVersion: string }) {
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 进行中的动作：'check' | 'install' | 'rollback' | 'waiting'（用于禁用按钮与进度展示） */
  const [phase, setPhase] = useState<'check' | 'install' | 'rollback' | 'waiting' | null>(null)
  const [log, setLog] = useState<string[]>([])
  /** 刚完成的更新 / 回滚（重启回来后展示的结果横幅，几秒后自动消失） */
  const [doneBanner, setDoneBanner] = useState<string | null>(null)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current)
    }
  }, [])

  const appendLog = (line: string) => setLog((prev) => [...prev, line])

  const describe = (e: unknown): string => {
    if (e instanceof ApiError) return e.message
    if (isNetworkError(e)) return '网络中断，请稍后重试'
    return String(e)
  }

  const loadStatus = async (withCheck: boolean) => {
    setChecking(withCheck)
    setError(null)
    try {
      const result = withCheck ? await api.updateCheck() : await api.updateStatus()
      setStatus(result)
      if (withCheck) appendLog(result.latest ? `远端最新版本：${result.latest.version}` : '远端没有可安装的版本发布')
    } catch (e) {
      setError(describe(e))
    } finally {
      setChecking(false)
      setPhase(null)
    }
  }

  // 首次挂载先拿本地状态（不打 GitHub）；有重启记录就展示结果横幅
  useEffect(() => {
    void loadStatus(false).then(() => {
      // pending_restart 在 /status 里也会带出来（读了就清），用于重启回来的横幅
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (status?.pending_restart) {
      const { action, version, from_version } = status.pending_restart
      setDoneBanner(
        action === 'update'
          ? `已更新 ${from_version} → ${version}，服务已重启`
          : `已回滚到 ${version}（来自 ${from_version}）`,
      )
      const t = window.setTimeout(() => setDoneBanner(null), 10_000)
      return () => window.clearTimeout(t)
    }
  }, [status?.pending_restart])

  /** 轮询 /api/health 直到后端回来（版本号变成目标版本），然后整页重载。 */
  const waitRestart = (targetVersion: string) => {
    setPhase('waiting')
    appendLog('服务正在重启，等待恢复…')
    let attempts = 0
    timerRef.current = window.setInterval(async () => {
      attempts += 1
      try {
        const health = await api.health()
        if (health.status === 'ok' && health.version === targetVersion) {
          if (timerRef.current) window.clearInterval(timerRef.current)
          appendLog(`已恢复到 ${health.version}，刷新页面…`)
          window.location.reload()
        }
        // 版本号没到目标：还在旧进程上（exec 未发生或仍在初始化），继续等
      } catch {
        // 重启窗口期：连接被拒 / 掐断，继续轮询
      }
      if (attempts > 120) {
        // 2 分钟还没回来：多半起不来了，停下让用户看日志
        if (timerRef.current) window.clearInterval(timerRef.current)
        setPhase(null)
        setError('服务 2 分钟内没有恢复，请查看服务日志排查启动失败原因')
      }
    }, 1000)
  }

  const runStream = (path: '/api/update/install' | '/api/update/rollback') => {
    setPhase(path === '/api/update/install' ? 'install' : 'rollback')
    setError(null)
    setLog([])
    appendLog(path === '/api/update/install' ? '开始更新…' : '开始回滚…')
    updateStream(path, (event) => {
      if (event.type === 'update.downloading') {
        appendLog(`下载 ${event.version}（${Math.round(event.size / 1024 / 1024)} MB）…`)
      } else if (event.type === 'update.restarting') {
        appendLog(`${event.action === 'update' ? '安装' : '回滚'}完成：${event.version}`)
        waitRestart(event.version)
      } else if (event.type === 'update.error') {
        appendLog(`失败：${event.message}`)
        setError(event.message)
        setPhase(null)
      }
    }).catch((e: unknown) => {
      // 流层面断开：若已进入 waiting 说明重启已开始，忽略；否则是真错误
      if (phase === 'waiting') return
      setError(describe(e))
      setPhase(null)
    })
  }

  const busy = phase !== null
  const updateAvailable = status?.update_available && status.latest && status.latest.version !== currentVersion

  return (
    <section className="stack">
      <h2 className="card-section-title">版本更新</h2>
      <p className="dim small">
        更新来源是 GitHub Release（版本级发布，不是 main 分支的日常提交）。托管部署
        （Docker / 安装包）可一键更新并自动重启；源码模式只提示新版本，升级走 git。
      </p>

      {doneBanner && <div className="banner ok">{doneBanner}</div>}
      {error && <div className="banner err">{error}</div>}

      <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
        <button className="btn sm" disabled={busy || checking} onClick={() => void loadStatus(true)}>
          {checking ? '◌ 检测中…' : '⟳ 检测更新'}
        </button>
        {status?.can_install && updateAvailable && (
          <button className="btn sm primary" disabled={busy} onClick={() => runStream('/api/update/install')}>
            {phase === 'install' || phase === 'waiting' ? '◌ 更新中…' : `↑ 更新到 ${status.latest?.version}`}
          </button>
        )}
        {status?.rollback_available && (
          <button className="btn sm ghost" disabled={busy} onClick={() => runStream('/api/update/rollback')}>
            {phase === 'rollback' ? '◌ 回滚中…' : '↓ 回滚到上一版本'}
          </button>
        )}
      </div>

      {status && (
        <div className="grid">
          <div className="card" style={{ gap: 6 }}>
            <span className="card-section-title">当前版本</span>
            <span>{currentVersion}</span>
          </div>
          <div className="card" style={{ gap: 6 }}>
            <span className="card-section-title">远端最新</span>
            <span>
              {status.latest ? (
                <>
                  {status.latest.version}
                  {status.latest.version === currentVersion && <span className="dim small">（已是最新）</span>}
                </>
              ) : (
                <span className="dim small">未检测（点「检测更新」）</span>
              )}
            </span>
          </div>
          <div className="card" style={{ gap: 6 }}>
            <span className="card-section-title">部署形态</span>
            <span>
              {status.mode === 'managed' ? '托管（支持一键更新 / 回滚）' : '源码（更新走 git）'}
              {status.installed_versions.length > 0 && (
                <span className="dim small"> · 已装 {status.installed_versions.join(' / ')}</span>
              )}
            </span>
          </div>
        </div>
      )}

      {status?.mode === 'source' && status.update_available && (
        <div className="banner info">
          远端有新版本 {status.latest?.version}。当前是源码模式，请在仓库里 <code className="mono-sm">git pull</code> 后重启服务。
        </div>
      )}

      {log.length > 0 && (
        <pre className="mono-sm" style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
          {log.join('\n')}
        </pre>
      )}

      {status?.latest?.notes && (
        <details>
          <summary className="dim small" style={{ cursor: 'pointer' }}>
            {status.latest.version} 更新说明
          </summary>
          <div className="card" style={{ marginTop: 8 }}>
            <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{status.latest.notes}</pre>
          </div>
        </details>
      )}
    </section>
  )
}
