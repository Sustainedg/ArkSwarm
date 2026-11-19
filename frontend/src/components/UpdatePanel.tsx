import { useEffect, useRef, useState } from 'react'
import { api, ApiError, updateStream } from '../lib/api'
import { isNetworkError } from '../lib/api'
import { DEFAULT_CHECK_HOUR, useAutoCheckUpdates } from '../lib/autoCheckUpdates'
import { desktopUpdates } from '../lib/desktop'
import { CheckIcon, SettingsIcon } from './Icons'
import { Modal } from './Modal'
import type { DesktopUpdateBridge, DesktopUpdateState, UpdateStatus } from '../lib/types'

/**
 * 「设置 → 关于」的版本更新面板。按运行形态分成两套：
 *
 * - **桌面端**（`window.arkswarmDesktop.updates` 存在）：与托盘「检查桌面端更新…」
 *   共用 electron-updater，下载的是**完整安装包**，下载完点「重启安装」。
 * - **网页 / Docker 部署**：走后端 GitHub Release 通道（`arkswarm.tar.gz`）。
 *
 * 后端通道的流程（安装 / 回滚是 SSE 长动作，最后进程会原地重启）：
 *   1. `POST /api/update/install` 逐条收 `update.*` 事件；
 *   2. 收到 `update.restarting` 后进入「等待服务回来」——轮询 `/api/health`，
 *      exec 重启有几秒不可用窗口，接口 200 且版本号变化即成功；
 *   3. 刷新整页（`location.reload()`）：前端产物也换新了，必须重载才拿到。
 *
 * 界面刻意只留版本号、状态、按钮 —— 部署形态 / 更新来源这些说明
 * 一律不写在这里（想知道的人自会去看文档）。日志小窗只在有动作时出现。
 */
export function UpdatePanel({ currentVersion }: { currentVersion: string }) {
  // 桌面端只有一条更新通道：下载**完整安装包**（与托盘菜单同一个 electron-updater）。
  // 浏览器 / Docker 部署仍走后端 `/api/update/*`（`arkswarm.tar.gz` 一键更新）。
  return desktopUpdates() ? (
    <DesktopUpdatePanel />
  ) : (
    <ServerUpdatePanel currentVersion={currentVersion} />
  )
}

/** 网页 / Docker 部署的版本更新面板（后端 Release 通道）。 */
function ServerUpdatePanel({ currentVersion }: { currentVersion: string }) {
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [checking, setChecking] = useState(false)
  /** 「更新设置」弹窗开合 */
  const [settingsOpen, setSettingsOpen] = useState(false)
  /** 自动检查更新：每日检查时刻（null = 关闭，本机偏好）。开着的话挂载时也顺手打一次远端检测。 */
  const autoCheck = useAutoCheckUpdates()
  const [error, setError] = useState<string | null>(null)
  /** 进行中的动作：'check' | 'install' | 'rollback' | 'waiting'（用于禁用按钮与进度展示） */
  const [phase, setPhase] = useState<'check' | 'install' | 'rollback' | 'waiting' | null>(null)
  const [log, setLog] = useState<string[]>([])
  /** 刚完成的更新 / 回滚（重启回来后展示的结果横幅，几秒后自动消失） */
  const [doneBanner, setDoneBanner] = useState<string | null>(null)
  const timerRef = useRef<number | null>(null)
  /** 日志窗口：新行进来自动滚到底（更新中用户盯着的就是「现在跑到哪一步了」）。 */
  const logRef = useRef<HTMLPreElement | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current)
    }
  }, [])

  useEffect(() => {
    const node = logRef.current
    if (node) node.scrollTop = node.scrollHeight
  }, [log])

  const appendLog = (line: string) => setLog((prev) => [...prev, line])

  const describe = (e: unknown): string => {
    if (e instanceof ApiError) return e.message
    if (isNetworkError(e)) return '网络中断，请稍后重试'
    return String(e)
  }

  const loadStatus = async (withCheck: boolean) => {
    setChecking(withCheck)
    setPhase(withCheck ? 'check' : null)
    setError(null)
    try {
      const result = withCheck ? await api.updateCheck() : await api.updateStatus()
      setStatus(result)
      // 检测结果不写日志：卡片上的「✓ 已是最新 / → 新版本号」就是结果本身，
      // 再往日志里记一条「远端最新版本：…」只会像一行删不掉的残留提示。
      // 日志只留给真正的长动作（下载 / 安装 / 等重启）。
    } catch (e) {
      setError(describe(e))
    } finally {
      setChecking(false)
      setPhase(null)
    }
  }

  // 首次挂载先拿本地状态；开着自动检查就顺手打一次远端检测（否则不打 GitHub）。
  useEffect(() => {
    void loadStatus(autoCheck.hour !== null)
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
  const upToDate = status?.latest && status.latest.version === currentVersion

  return (
    <section className="stack">
      <h2 className="card-section-title">版本更新</h2>

      {doneBanner && (
        <div className="banner ok update-done">
          <span className="update-done-check">
            <CheckIcon size={14} />
          </span>
          {doneBanner}
        </div>
      )}
      {error && <div className="banner err">{error}</div>}

      {/* 一张卡三个元素：当前版本号、状态（最新 / 有新版本 / 进行中）、按钮。
          有新版本时右侧给「→ 新版本号」；动作进行中整卡顶部跑流动光条。 */}
      <div className={`update-card${phase ? ' busy' : ''}`}>
        {phase && <span className="update-card-progress" aria-hidden />}
        <div className="update-card-row">
          <span className="update-current">{currentVersion}</span>
          {updateAvailable && (
            <span className="update-arrow hot" aria-hidden>
              →
            </span>
          )}
          {updateAvailable && <span className="update-next">{status?.latest?.version}</span>}
          {upToDate && (
            <span className="update-up-to-date">
              <CheckIcon size={13} /> 已是最新
            </span>
          )}
          <span className="update-actions">
            <button className="btn sm" disabled={busy || checking} onClick={() => void loadStatus(true)}>
              {checking ? '检测中…' : '检测更新'}
            </button>
            {status?.can_install && updateAvailable && (
              <button className="btn sm primary" disabled={busy} onClick={() => runStream('/api/update/install')}>
                {phase === 'install' || phase === 'waiting' ? '更新中…' : '更新'}
              </button>
            )}
            {status?.rollback_available && (
              <button className="btn sm ghost" disabled={busy} onClick={() => runStream('/api/update/rollback')}>
                {phase === 'rollback' ? '回滚中…' : '回滚'}
              </button>
            )}
            {/* 更新设置：齿轮，与动作按钮一道贴右 —— 它们都是「对版本号做的事」 */}
            <button
              className="update-settings-btn"
              onClick={() => setSettingsOpen(true)}
              aria-label="更新设置"
              aria-haspopup="dialog"
              title="更新设置（自动检查的时刻）"
            >
              <SettingsIcon size={16} />
            </button>
          </span>
        </div>
        {status?.mode === 'source' && updateAvailable && (
          <p className="update-source-hint">
            源码模式：请在仓库里 <code className="mono-sm">git pull</code> 后重启服务。
          </p>
        )}
      </div>

      {/* 日志小窗：只在有动作时出现，静悄悄的界面不留一块空终端。 */}
      {log.length > 0 && (
        <pre ref={logRef} className="update-log mono-sm">
          {log.join('\n')}
        </pre>
      )}

      {/* 更新设置弹窗：自动检查的开关 + 每天几点检查（本机偏好）。 */}
      {settingsOpen && (
        <UpdateSettingsModal
          hour={autoCheck.hour}
          onSave={(next) => {
            autoCheck.setHour(next)
            setSettingsOpen(false)
          }}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </section>
  )
}

/**
 * 桌面端的版本更新面板：与托盘「检查桌面端更新…」共用同一个 electron-updater 状态机。
 *
 * 更新形态只有一种 —— **下载完整安装包**（Windows NSIS / macOS zip；Windows 支持
 * `.blockmap` 差分，只传变化的数据块）。不存在「只换后端源码」的轻量通道：
 * 新安装包里的后端代码会在下次启动时由 bootstrap.ts 物化并切 current。
 */
function DesktopUpdatePanel() {
  // 初始化时抓一次桥对象：contextBridge 的嵌套对象不保证引用稳定，
  // 直接放进 useEffect 依赖会导致每轮渲染都重新订阅。
  const [bridge] = useState(() => desktopUpdates() as DesktopUpdateBridge);
  const [state, setState] = useState<DesktopUpdateState | null>(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    let alive = true;
    void bridge
      .status()
      .then((next) => {
        if (alive) setState(next);
      })
      .catch(() => undefined);
    // 后台检查 / 下载的进度由主进程推送，页面实时刷新
    const off = bridge.onState((next) => setState(next));
    return () => {
      alive = false;
      off();
    };
  }, [bridge]);

  const check = async () => {
    setChecking(true);
    try {
      setState(await bridge.check());
    } catch {
      // IPC 层失败（主进程异常 / 桥不可用）：进度事件拿不到时至少别让按钮卡死
      setState((prev) =>
        prev ? { ...prev, phase: 'error', error: '更新检查失败，请稍后重试' } : prev,
      );
    } finally {
      setChecking(false);
    }
  };

  const phase = state?.phase ?? 'idle';
  const percent = state?.percent ?? 0;
  const downloaded = phase === 'downloaded';
  const updating = checking || phase === 'checking' || phase === 'downloading';
  const unsupported = phase === 'unsupported';
  const upToDate = phase === 'up-to-date';
  const updateAvailable = Boolean(
    state?.available_version && state.available_version !== state.current_version,
  );

  return (
    <section className="stack">
      <h2 className="card-section-title">版本更新</h2>

      {state?.error && <div className="banner err">{state.error}</div>}

      <div className={`update-card${updating ? ' busy' : ''}`}>
        {updating && <span className="update-card-progress" aria-hidden />}
        <div className="update-card-row">
          <span className="update-current">{state?.current_version || '…'}</span>
          {updateAvailable && (
            <>
              <span className="update-arrow hot" aria-hidden>
                →
              </span>
              <span className="update-next">{state?.available_version}</span>
            </>
          )}
          {upToDate && (
            <span className="update-up-to-date">
              <CheckIcon size={13} /> 已是最新
            </span>
          )}
          {phase === 'downloading' && <span className="dim small">下载中 {percent}%</span>}
          {downloaded && <span className="dim small">安装包已下载</span>}
          <span className="update-actions">
            <button className="btn sm" disabled={updating || unsupported} onClick={() => void check()}>
              {updating ? '检测中…' : '检测更新'}
            </button>
            {downloaded && (
              <button className="btn sm primary" onClick={() => void bridge.install()}>
                重启安装
              </button>
            )}
          </span>
        </div>
        <p className="update-source-hint">
          桌面端更新会下载完整安装包（Windows 支持差分下载，只传变化的数据块）；
          下载完成后点「重启安装」生效。
        </p>
      </div>
    </section>
  );
}

/** 更新设置弹窗：开关一行 + 时刻选择做成卡片（大字时刻 + 说明），排版分主次。 */
function UpdateSettingsModal({
  hour,
  onSave,
  onClose,
}: {
  hour: number | null
  onSave: (hour: number | null) => void
  onClose: () => void
}) {
  const [enabled, setEnabled] = useState(hour !== null)
  const [pick, setPick] = useState(hour ?? DEFAULT_CHECK_HOUR)
  return (
    <Modal
      title="更新设置"
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={() => onSave(enabled ? pick : null)}>
            保存
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        {/* 主开关：一行说明 + 开关本身，左右分开（标题在左、勾选框在右，常见的设置行） */}
        <label className="update-toggle">
          <span className="update-toggle-main">
            <span className="update-toggle-title">自动检查更新</span>
            <span className="update-toggle-sub">每天在设定时刻检测一次 GitHub Release</span>
          </span>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            aria-label="开启自动检查更新"
          />
        </label>
        {/* 时刻选择：开起来才出现。当前时刻大字 + 左右微调，比一列 24 项的下拉更直观 */}
        {enabled && (
          <div className="update-hour-card">
            <span className="update-hour-label">每天检查时刻</span>
            <div className="update-hour-picker">
              <button
                type="button"
                className="update-hour-step"
                onClick={() => setPick((h) => (h + 23) % 24)}
                aria-label="提前一小时"
              >
                −
              </button>
              <span className="update-hour-value">{String(pick).padStart(2, '0')}:00</span>
              <button
                type="button"
                className="update-hour-step"
                onClick={() => setPick((h) => (h + 1) % 24)}
                aria-label="推迟一小时"
              >
                ＋
              </button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}
