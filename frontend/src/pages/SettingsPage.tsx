import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { AccountPanel } from '../components/AccountPanel'
import { UserAdminPanel } from '../components/UserAdminPanel'
import { CredentialVault } from '../components/CredentialVault'
import { GatewayManager } from '../components/GatewayManager'
import { ImSettings } from '../components/ImSettings'
import { KernelManager } from '../components/KernelManager'
import { ProxySettings } from '../components/ProxySettings'
import { TitleSettingsSection } from '../components/TitleSettings'
import { UpdatePanel } from '../components/UpdatePanel'
import { Modal } from '../components/Modal'
import { api, ApiError } from '../lib/api'
import { useAuth } from '../components/AuthGate'
import { THEME_OPTIONS, useThemeMode, withDocumentTheme } from '../lib/theme'
import type { ActiveCollabRun, ArchitectureDoc, Overview } from '../lib/types'

/**
 * 设置页的标签页：**按设置类型分组**，一页只放一类设置。
 *
 * 「关于」收的是**只读信息**（系统信息 · 正在运行的智能体 · 版本记录）；
 * 「内核管理」管各 CLI 内核本身（装没装 / 什么版本 / 安装 · 更新 · 卸载）。
 * 顺序按「日常会动的在前」排：外观 / 对话最常改，内核管理与关于基本只看。
 */
const ALL_TABS = [
  { id: 'appearance', label: '外观' },
  { id: 'account', label: '账号' },
  { id: 'users', label: '用户' },
  { id: 'chat', label: '对话' },
  { id: 'gateways', label: '网关' },
  { id: 'credentials', label: '凭据' },
  { id: 'im', label: 'IM' },
  { id: 'network', label: '网络' },
  { id: 'kernels', label: '内核管理' },
  { id: 'about', label: '关于' },
] as const

//: 标签页定义的旧名字（守卫测试按这个名字找）；实际显示时会按角色过滤掉「用户」。
const TABS = ALL_TABS

type SettingsTab = (typeof ALL_TABS)[number]['id']

function isSettingsTab(value: string | null): value is SettingsTab {
  return ALL_TABS.some((item) => item.id === value)
}

/**
 * 设置（桌面端）：按设置类型分标签页。当前页写进 `?tab=` —— 刷新、分享链接，
 * 以及老地址 `/runtimes → /settings?tab=kernels` 的重定向都能落到正确的页。
 *
 * 标签页只做「分组」，各项设置的组件与文档语义都没变 —— 每个组件仍自带标题与说明，
 * 换页只是把它们放到不同页里。移动端的设置页**不跟这套**：它刻意只留「现在要动的开关」。
 */
export function SettingsPage() {
  const navigate = useNavigate()
  const { isAdmin } = useAuth()
  const { mode, theme, setMode } = useThemeMode()
  const [params, setParams] = useSearchParams()
  const current = params.get('tab')
  const requested: SettingsTab = isSettingsTab(current) ? current : 'appearance'
  // 「用户」只有管理员能看；非管理员直接落到外观，避免链接能打开一个空页。
  const tab: SettingsTab = requested === 'users' && !isAdmin ? 'appearance' : requested
  const tabs = TABS.filter((item) => item.id !== 'users' || isAdmin)
  const [overview, setOverview] = useState<Overview | null>(null)
  /** 协作里的运行是另一张登记表（`/api/collab/active-runs`），不在 overview.active_runs 里 */
  const [collabRuns, setCollabRuns] = useState<ActiveCollabRun[]>([])
  const [error, setError] = useState<string | null>(null)
  const [architecture, setArchitecture] = useState<ArchitectureDoc | null>(null)
  const [archOpen, setArchOpen] = useState(false)
  const [archError, setArchError] = useState<string | null>(null)

  const setTab = (next: SettingsTab) => {
    // replace：切标签不该往浏览器历史里塞一串记录 —— 退一次就离开设置页更符合直觉。
    setParams(next === 'appearance' ? {} : { tab: next }, { replace: true })
  }

  const reload = async () => {
    try {
      // 两处一起取：对话域的运行表 + 协作域的运行表（协作的智能体不在前者里）。
      const [info, active] = await Promise.all([api.overview(), api.listActiveCollabRuns()])
      setOverview(info)
      setCollabRuns(active)
      setError(null)
    } catch (e) {
      setError(describe(e))
    }
  }

  // 「关于」要每 8 秒刷新「正在运行的智能体」（状态与时长自己在走）。
  // 其余标签页用不到概览 —— 别在打开「外观」时白跑每 8 秒一次的请求。
  useEffect(() => {
    if (tab !== 'about') return
    void reload()
    const timer = setInterval(() => void reload(), 8000)
    return () => clearInterval(timer)
    // reload 每次渲染都是新的，这里只盯标签页
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab])

  const openArchitecture = async () => {
    setArchOpen(true)
    if (architecture) return
    setArchError(null)
    try {
      setArchitecture(await api.architecture())
    } catch (e) {
      setArchError(describe(e))
    }
  }

  const versionHistory = overview?.version_history ?? []

  return (
    <>
      <header className="page-header settings-head">
        <div>
          <h1 className="page-title">设置</h1>
          <p className="page-desc">
            按类型分页：外观 · 账号{isAdmin ? ' · 用户' : ''} · 对话 · 网关 · 凭据 · IM · 网络 · 内核管理 · 关于。
          </p>
        </div>
      </header>

      {/* 标签栏独占一行：7 个标签，塞在页头右侧会被挤成一团（标签样式见 .tabs / .tab）。 */}
      <div className="settings-tabbar">
        <div className="tabs">
          {tabs.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`tab${tab === item.id ? ' active' : ''}`}
              aria-pressed={tab === item.id}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="page-body stack">
        {error && <div className="banner err">{error}</div>}

        {tab === 'appearance' && (
          <section className="stack">
            <h2 className="card-section-title">主题</h2>
            <div className="theme-picker">
              {THEME_OPTIONS.map((option) => {
                const active = mode === option.value
                return (
                  <button
                    key={option.value}
                    className={`theme-option${active ? ' active' : ''}`}
                    aria-pressed={active}
                    onClick={() => setMode(option.value)}
                  >
                    <span className="theme-option-head">
                      <span className="theme-option-label">{option.label}</span>
                      {active && <span className="theme-option-check">✓</span>}
                    </span>
                    <span className="theme-option-hint">{option.hint}</span>
                  </button>
                )
              })}
            </div>
            <p className="dim small">外观是这台设备的个人偏好，只存在本机，不影响别人。</p>
          </section>
        )}

        {tab === 'account' && <AccountPanel />}

        {tab === 'users' && isAdmin && <UserAdminPanel />}

        {/* 「对话」与「网络」是平台级设置（后端只允许管理员写入）：成员看得到说明，不给编辑入口。 */}
        {tab === 'chat' &&
          (isAdmin ? (
            <TitleSettingsSection />
          ) : (
            <section className="card">
              <div className="card-title">自动起标题由管理员配置</div>
              <p className="dim small">这一项影响全平台，只有管理员能修改。</p>
            </section>
          ))}

        {tab === 'gateways' && <GatewayManager />}

        {tab === 'credentials' && <CredentialVault />}

        {/* IM 是通道：每条连接绑一个智能体，私聊直接进它当前那段对话。 */}
        {tab === 'im' && <ImSettings />}

        {tab === 'network' &&
          (isAdmin ? (
            <ProxySettings />
          ) : (
            <section className="card">
              <div className="card-title">网络代理由管理员配置</div>
              <p className="dim small">这一项影响全平台的安装 / 更新，只有管理员能修改。</p>
            </section>
          ))}

        {tab === 'kernels' && <KernelManager />}

        {tab === 'about' && (
          <>
            {isAdmin && <UpdatePanel currentVersion={overview?.version ?? '…'} />}
            <section className="stack">
              <div className="row" style={{ gap: 8 }}>
                <h2 className="card-section-title" style={{ flex: 1 }}>
                  系统信息
                </h2>
                <button className="btn sm" onClick={() => void openArchitecture()}>
                  🏛 系统架构
                </button>
                <button className="btn sm" onClick={() => void reload()}>
                  ↻ 刷新
                </button>
              </div>
              {overview ? (
                <div className="grid">
                  <Stat label="版本" value={overview.version} />
                  <Stat label="适配器" value={`${overview.installed_types} / ${overview.agent_types} 可用`} />
                  <Stat label="数据目录" value={overview.data_dir} mono />
                  <Stat label="工作目录根" value={overview.workspaces_root} mono />
                </div>
              ) : (
                <Loading />
              )}
            </section>

            <section className="stack">
              <h2 className="card-section-title">正在运行的智能体</h2>
              <p className="dim small">
                对话里的运行与协作里的运行分开登记在各自的运行表里，所以这里把两处一起列出来。
              </p>
              {overview && overview.active_runs.length + collabRuns.length > 0 ? (
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
                  {collabRuns.map((run) => (
                    <button
                      type="button"
                      className="card card-clickable"
                      key={`${run.kind}-${run.run_id}-${run.agent_name}`}
                      onClick={() => navigate(`/collab/runs/${run.run_id}`)}
                      title="打开这条协作运行"
                    >
                      <div className="card-head">
                        <div style={{ minWidth: 0 }}>
                          <div className="card-title">{run.agent_name}</div>
                          <div className="card-sub">
                            {run.kind === 'chat' ? '入口智能体的对话回复' : '协作工单步骤'}
                            {run.title ? ` · ${run.title}` : ''}
                          </div>
                        </div>
                        <span className="pill brand" style={{ marginLeft: 'auto' }}>
                          <span className="dot" />
                          协作 · {run.elapsed_seconds}s
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
              ) : (
                <p className="dim small">当前没有运行中的任务。</p>
              )}
            </section>

            {versionHistory.length > 0 && (
              <section className="stack">
                <h2 className="card-section-title">版本记录</h2>
                <div className="version-timeline">
                  {versionHistory.map((release) => (
                    <article
                      className={`version-item${release.version === overview?.version ? ' current' : ''}`}
                      key={release.version}
                    >
                      <span className="version-marker" />
                      <div className="version-content">
                        <div className="version-head">
                          <span className={`pill${release.version === overview?.version ? ' brand' : ''}`}>
                            {release.version}
                          </span>
                          <span className="version-title">{release.title}</span>
                          <span className="dim small version-date">{release.date}</span>
                        </div>
                        <ul className="version-highlights">
                          {release.highlights.map((item) => (
                            <li key={item}>{item}</li>
                          ))}
                        </ul>
                      </div>
                    </article>
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </div>

      {archOpen && (
        <Modal
          title="系统架构"
          wide
          extraClass="arch"
          onClose={() => setArchOpen(false)}
          footer={
            <button className="btn ghost" onClick={() => setArchOpen(false)}>
              关闭
            </button>
          }
        >
          <p className="dim small">
            内容实时渲染自文档 <code className="mono-sm">{architecture?.source ?? 'docs/architecture.md'}</code>
            ，本地更新文档后重新打开即可看到最新版本。
          </p>
          {archError && <div className="banner err">{archError}</div>}
          {architecture ? (
            <iframe
              className="arch-frame"
              title="系统架构"
              srcDoc={withDocumentTheme(architecture.html, theme)}
            />
          ) : (
            !archError && (
              <div className="row">
                <span className="spin">◌</span> 正在生成系统架构…
              </div>
            )
          )}
        </Modal>
      )}
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

/** 概览还没回来时的占位：这些标签页只在需要时才拉数据，别让页面看起来是空的。 */
function Loading() {
  return (
    <div className="row">
      <span className="spin">◌</span> 正在加载…
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
