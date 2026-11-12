import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { THEME_OPTIONS, useThemeMode, withDocumentTheme } from '../../lib/theme'
import type { ArchitectureDoc } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { refreshShell, useInstallController } from '../pwa'
import { describeError } from '../util'

/**
 * 设置：外观 + 配置入口 + 少数维护动作。
 *
 * 手机上只留「现在要动的开关」：运行概览、版本记录这些信息在桌面端看更合适，
 * 小屏上重复一遍只会把真正要用的入口挤下去。
 */
export function MobileSettingsPage({ version }: { version: string }) {
  const navigate = useNavigate()
  const install = useInstallController()
  const { mode, theme, setMode } = useThemeMode()

  const [architecture, setArchitecture] = useState<ArchitectureDoc | null>(null)
  const [archOpen, setArchOpen] = useState(false)
  const [archError, setArchError] = useState<string | null>(null)

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

  return (
    <div className="m-page">
      <MobileTopBar
        title="设置"
        badge={version ? <span className="m-badge">{version}</span> : undefined}
        subtitle="外观与配置入口"
      />

      <div className="m-page-body">
        <div className="stack">
          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">外观</div>
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
            <p className="dim small">外观只存在本机，不影响别人。</p>
          </div>

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">配置</div>
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
                <span className="m-item-sub">
                  内核的安装 / 更新 / 卸载，以及每个内核的智能体设置（网关 · 密钥 · 模型）
                </span>
              </span>
              <span className="m-item-chevron">›</span>
            </button>
          </div>

          <div className="stack" style={{ gap: 8 }}>
            <div className="card-section-title">更多</div>
            <div className="row wrap" style={{ gap: 8 }}>
              {!install.standalone && install.nativeReady && (
                <button className="btn sm primary" onClick={() => void install.install()}>
                  添加到主屏
                </button>
              )}
              <button className="btn sm" onClick={() => void refreshShell()}>
                更新到最新版本
              </button>
              <button className="btn sm" onClick={() => void openArchitecture()}>
                系统架构
              </button>
              <a className="btn sm ghost" href="/">
                切换到桌面端
              </a>
            </div>
            {!install.standalone && install.ios && (
              <p className="dim small">iPhone / iPad：在 Safari 的「分享」里选「添加到主屏幕」。</p>
            )}
            <p className="dim small">
              「更新到最新版本」会清掉离线缓存并重新加载 —— 独立应用（添加到主屏）里没有刷新按钮，这是
              卡在旧外壳时的出口。
            </p>
          </div>
        </div>
      </div>

      {archOpen && (
        <MobileSheet full title="系统架构" onClose={() => setArchOpen(false)}>
          {archError && <div className="banner err">{archError}</div>}
          {architecture ? (
            <iframe
              className="m-arch-frame"
              title="系统架构"
              srcDoc={withDocumentTheme(architecture.html, theme)}
            />
          ) : (
            !archError && <p className="dim small">正在加载架构文档…</p>
          )}
        </MobileSheet>
      )}
    </div>
  )
}
