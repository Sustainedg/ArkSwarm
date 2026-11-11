import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import type { AgentType, ArchitectureDoc, Overview } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { refreshShell, useInstallController } from '../pwa'
import { describeError, formatDuration } from '../util'

/** 设置与系统信息：智能体入口、运行概览、版本记录、架构文档与安装引导。 */
export function MobileSettingsPage({ version }: { version: string }) {
  const navigate = useNavigate()
  const install = useInstallController()

  const [overview, setOverview] = useState<Overview | null>(null)
  const [types, setTypes] = useState<AgentType[]>([])
  const [error, setError] = useState<string | null>(null)
  const [architecture, setArchitecture] = useState<ArchitectureDoc | null>(null)
  const [archOpen, setArchOpen] = useState(false)
  const [archError, setArchError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    try {
      const [info, typeList] = await Promise.all([api.overview(), api.agentTypes()])
      setOverview(info)
      setTypes(typeList)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    }
  }, [])

  useEffect(() => {
    void reload()
    const timer = setInterval(() => void reload(), 15000)
    return () => clearInterval(timer)
  }, [reload])

  const openArchitecture = async () => {
    setArchOpen(true)
    if (architecture) return
    setArchError(null)
    try {
      setArchitecture(await api.architecture())
    } catch (e) {
      setArchError(describeError(e))
    }
  }

  const versionHistory = overview?.version_history ?? []

  return (
    <div className="m-page">
      <MobileTopBar
        title="设置"
        badge={version ? <span className="m-badge">{version}</span> : undefined}
        subtitle="系统概览与入口"
        actions={
          <button className="m-icon-btn" onClick={() => void reload()} aria-label="刷新">
            ↻
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">配置</div>
            <button className="m-item" onClick={() => navigate('/agents')}>
              <span style={{ fontSize: 18 }}>⚙️</span>
              <span className="m-item-main">
                <span className="m-item-title">智能体配置</span>
                <span className="m-item-sub">
                  适配器 · 网关 · API 密钥 · 模型，共 {overview ? types.length : '—'} 种适配器
                </span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <button className="m-item" onClick={() => navigate('/skills')}>
              <span style={{ fontSize: 18 }}>🧩</span>
              <span className="m-item-main">
                <span className="m-item-title">技能</span>
                <span className="m-item-sub">统一编目各内核的技能，通过角色分配生效</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <button className="m-item" onClick={() => navigate('/runtimes')}>
              <span style={{ fontSize: 18 }}>🧬</span>
              <span className="m-item-main">
                <span className="m-item-title">运行时</span>
                <span className="m-item-sub">查看各内核的安装状态与版本，可安装 / 更新 / 卸载</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
          </div>

          {overview && (
            <div className="stack" style={{ gap: 8 }}>
              <div className="card-section-title">运行概览</div>
              <div className="grid">
                <div className="m-stat">
                  <div className="m-stat-label">适配器</div>
                  <div className="m-stat-value">
                    {overview.installed_types} / {overview.agent_types} 可用
                  </div>
                </div>
                <div className="m-stat">
                  <div className="m-stat-label">运行中任务</div>
                  <div className="m-stat-value">{overview.active_runs.length}</div>
                </div>
              </div>
              <div className="card">
                <div className="row wrap" style={{ gap: 8 }}>
                  <span className="muted small">数据目录</span>
                  <span className="mono-sm dim">{overview.data_dir}</span>
                </div>
                <div className="row wrap" style={{ gap: 8 }}>
                  <span className="muted small">工作目录根</span>
                  <span className="mono-sm dim">{overview.workspaces_root}</span>
                </div>
              </div>

              {overview.active_runs.length > 0 && (
                <div className="m-list">
                  {overview.active_runs.map((run) => (
                    <div className="m-item" key={run.id} style={{ cursor: 'default' }}>
                      <span className="m-item-main">
                        <span className="m-item-title">{run.agent_name}</span>
                        <span className="m-item-sub mono-sm">{run.id}</span>
                      </span>
                      <span className="pill ok">
                        {run.status} · {formatDuration(run.elapsed_seconds)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">应用</div>
            <div className="card">
              <div className="row">
                <span style={{ fontSize: 18 }}>📲</span>
                <div className="m-item-main">
                  <div className="m-item-title">添加到主屏</div>
                  <div className="m-item-sub">
                    {install.standalone
                      ? '已作为独立应用运行'
                      : install.ios
                        ? '在「分享」里选择「添加到主屏幕」'
                        : '安装后可全屏运行，离线也能打开界面'}
                  </div>
                </div>
                {!install.standalone && install.nativeReady && (
                  <button className="btn sm primary" onClick={() => void install.install()}>
                    安装
                  </button>
                )}
              </div>
            </div>
            <button className="m-item" onClick={() => void refreshShell()}>
              <span style={{ fontSize: 18 }}>♻️</span>
              <span className="m-item-main">
                <span className="m-item-title">更新到最新版本</span>
                <span className="m-item-sub">清掉离线缓存并重新加载（独立应用里没有刷新按钮）</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
            <a className="m-item" href="/">
              <span style={{ fontSize: 18 }}>🖥️</span>
              <span className="m-item-main">
                <span className="m-item-title">切换到桌面端</span>
                <span className="m-item-sub">在电脑上打开完整控制台（含可视化协作编排）</span>
              </span>
              <span className="m-item-chevron">›</span>
            </a>
            <button className="m-item" onClick={() => void openArchitecture()}>
              <span style={{ fontSize: 18 }}>🏛</span>
              <span className="m-item-main">
                <span className="m-item-title">系统架构</span>
                <span className="m-item-sub">分层、事件协议与扩展点说明</span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
          </div>

          {versionHistory.length > 0 && (
            <div className="stack" style={{ gap: 8 }}>
              <div className="card-section-title">版本记录</div>
              {versionHistory.map((release) => (
                <div className="card" key={release.version}>
                  <div className="row wrap" style={{ gap: 6 }}>
                    <span className={`pill${release.version === overview?.version ? ' brand' : ''}`}>
                      {release.version}
                    </span>
                    <span style={{ fontWeight: 600, fontSize: 13.5 }}>{release.title}</span>
                    <span className="dim small" style={{ marginLeft: 'auto' }}>{release.date}</span>
                  </div>
                  <ul style={{ listStyle: 'disc', paddingLeft: '1.2em', color: 'var(--text-muted)', fontSize: 12.5, lineHeight: 1.7 }}>
                    {release.highlights.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {archOpen && (
        <MobileSheet full title="系统架构" onClose={() => setArchOpen(false)}>
          {archError && <div className="banner err">{archError}</div>}
          {architecture ? (
            <iframe className="m-arch-frame" title="系统架构" srcDoc={architecture.html} />
          ) : (
            !archError && <p className="dim small">正在加载架构文档…</p>
          )}
        </MobileSheet>
      )}
    </div>
  )
}
