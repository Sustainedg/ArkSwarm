import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import type { Conversation, Role } from '../../lib/types'
import { sameConversationList, useAutoRefresh } from '../../lib/useAutoRefresh'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { useInstallController } from '../pwa'
import { describeError, relativeTime } from '../util'

const INSTALL_HINT_KEY = 'magenta.m.installHintDismissed'

/** 对话列表：移动端的首页。点开进入沉浸式对话，右下角悬浮按钮新建对话。 */
export function MobileConversationsPage({ version }: { version: string }) {
  const navigate = useNavigate()
  const install = useInstallController()

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [hintDismissed, setHintDismissed] = useState(() => localStorage.getItem(INSTALL_HINT_KEY) === '1')

  const load = useCallback(async () => {
    try {
      const [list, roleList] = await Promise.all([api.listConversations(), api.listRoles()])
      setConversations((prev) => (sameConversationList(prev, list) ? prev : list))
      setRoles(roleList)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useAutoRefresh(load, { intervalMs: 8000 })

  const dismissHint = () => {
    localStorage.setItem(INSTALL_HINT_KEY, '1')
    setHintDismissed(true)
  }

  const create = async (roleId: string) => {
    setCreating(true)
    setError(null)
    try {
      const conversation = await api.createConversation({ role_id: roleId })
      setPickerOpen(false)
      await load()
      navigate(`/chat/${conversation.id}`)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setCreating(false)
    }
  }

  const showInstallHint = !install.standalone && !hintDismissed

  return (
    <div className="m-page">
      <MobileTopBar
        leading={<div className="m-brand-mark">✦</div>}
        title="Magenta Agents"
        badge={version ? <span className="m-badge">{version}</span> : undefined}
        subtitle="多智能体控制台 · 移动端"
        actions={
          <button className="m-icon-btn" onClick={() => void load()} aria-label="刷新">
            ↻
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {showInstallHint && (
            <div className="m-install">
              <span style={{ fontSize: 20 }}>📲</span>
              <div className="m-install-text">
                <div style={{ fontWeight: 600 }}>把 Magenta 装到主屏</div>
                <div className="dim">
                  {install.ios
                    ? '点浏览器底部的「分享」，选择「添加到主屏幕」，之后就能像 App 一样全屏打开。'
                    : '安装后可全屏运行，离线也能打开界面；对话仍需联网。'}
                </div>
                <div className="m-install-actions">
                  {install.nativeReady && (
                    <button className="btn sm primary" onClick={() => void install.install()}>
                      立即安装
                    </button>
                  )}
                  <button className="btn sm ghost" onClick={dismissHint}>
                    暂不
                  </button>
                </div>
              </div>
            </div>
          )}

          {error && <div className="banner err">{error}</div>}

          {loading ? (
            <p className="dim small">正在加载对话…</p>
          ) : conversations.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">💬</div>
              <p>还没有对话</p>
              <p className="dim small">先创建角色，再开始第一段对话。</p>
              <button className="btn sm" style={{ marginTop: 6 }} onClick={() => navigate('/roles')}>
                去创建角色
              </button>
            </div>
          ) : (
            <div className="m-list">
              {conversations.map((conversation) => (
                <button
                  key={conversation.id}
                  className="m-item"
                  onClick={() => navigate(`/chat/${conversation.id}`)}
                >
                  <span className="avatar">{conversation.role_avatar ?? '🤖'}</span>
                  <span className="m-item-main">
                    <span className="m-item-title">{conversation.title}</span>
                    <span className="m-item-sub">
                      {conversation.role_name} · {conversation.message_count} 条 ·{' '}
                      {relativeTime(conversation.updated_at)}
                    </span>
                  </span>
                  <span className="m-item-chevron">›</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <button className="btn primary m-fab" onClick={() => setPickerOpen(true)}>
        ＋ 新建
      </button>

      {pickerOpen && (
        <MobileSheet title="选择角色开始对话" onClose={() => setPickerOpen(false)}>
          {roles.length === 0 ? (
            <div className="banner info">
              还没有角色。角色 = 人格设定 + 绑定的智能体，请先到「角色」页创建。
            </div>
          ) : (
            <div className="m-list">
              {roles.map((role) => (
                <button
                  key={role.id}
                  className="m-item"
                  disabled={creating}
                  onClick={() => void create(role.id)}
                >
                  <span className="avatar">{role.avatar}</span>
                  <span className="m-item-main">
                    <span className="m-item-title">{role.name}</span>
                    <span className="m-item-sub">{role.agent_name} · {role.tagline ?? '无备注'}</span>
                  </span>
                  <span className="pill brand">开始</span>
                </button>
              ))}
            </div>
          )}
        </MobileSheet>
      )}
    </div>
  )
}
