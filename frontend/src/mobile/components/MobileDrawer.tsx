import { useCallback, useEffect, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../../lib/api'
import { formatListTime } from '../../lib/datetime'
import type { Conversation, Role } from '../../lib/types'
import { sameConversationList, useAutoRefresh } from '../../lib/useAutoRefresh'
import { describeError } from '../util'

/** 抽屉上半部分的主导航：对应曾经固定在底部的 Tab（「对话」是回聊天页的入口）。 */
const NAV = [
  { to: '/chat', icon: '💬', label: '对话' },
  { to: '/roles', icon: '🎭', label: '角色' },
  { to: '/collab', icon: '🤝', label: '协作组' },
  { to: '/skills', icon: '🧩', label: '技能' },
  { to: '/settings', icon: '⚙️', label: '设置' },
]

interface Props {
  open: boolean
  onClose: () => void
  version?: string
}

/**
 * 左侧抽屉：上半部分是主导航，下半部分是对话列表（点左上角的 logo 打开）。
 *
 * 移动端只有这一处主导航 —— 底部的 Tab 栏因此取消，把屏幕高度让给内容；
 * 对话列表也从聊天页里挪到这里，聊天页打开就是聊天。
 *
 * 每个对话行右侧的「⋯」是会话级操作（重新生成 / 重置记忆 / 复制工作目录 / 删除）的入口：
 * 它把信号（路由 state）交给聊天页去弹面板 —— 那几件事要用到那边的消息与会话状态。
 *
 * 对话行只留文字（标题 + 角色/条数 + 时间）与那个「⋯」：一行一行的头像在这里只是噪音，
 * 去掉之后列表更紧凑，字号也比其它列表小一档（见 mobile.css 的 `.m-conv-row`）。
 */
export function MobileDrawer({ open, onClose, version }: Props) {
  const navigate = useNavigate()
  const { pathname } = useLocation()

  const [conversations, setConversations] = useState<Conversation[]>([])
  const [roles, setRoles] = useState<Role[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  /** true 时下半部分换成「选角色新建对话」 */
  const [pickerOpen, setPickerOpen] = useState(false)

  const activeId = /^\/chat\/([^/]+)/.exec(pathname)?.[1] ?? null

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
    if (open) void load()
  }, [open, load])

  // 抽屉开着时保持最新：另一台设备新发的消息会让标题、条数与时间自己变。
  useAutoRefresh(load, { intervalMs: 8000, enabled: open })

  // 收起后回到对话列表，下次打开不会停在上次的「选角色」那一步。
  useEffect(() => {
    if (!open) setPickerOpen(false)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const start = async (roleId: string) => {
    setCreating(true)
    setError(null)
    try {
      const conversation = await api.createConversation({ role_id: roleId })
      onClose()
      navigate(`/chat/${conversation.id}`)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setCreating(false)
    }
  }

  if (!open) return null

  return (
    <div className="m-drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside className="m-drawer" role="dialog" aria-modal="true" aria-label="导航与对话列表">
        <div className="m-drawer-head">
          <span className="m-brand-mark">✦</span>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div className="m-drawer-title">
              Magenta Agents
              {version && <span className="m-badge">{version}</span>}
            </div>
            <div className="m-drawer-sub">多智能体控制台</div>
          </div>
          <button className="m-icon-btn" onClick={onClose} aria-label="关闭菜单">
            ✕
          </button>
        </div>

        <nav className="m-drawer-nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `m-drawer-link${isActive ? ' active' : ''}`}
              onClick={onClose}
            >
              <span className="m-drawer-link-icon">{item.icon}</span>
              <span>{item.label}</span>
              <span className="m-drawer-chevron">›</span>
            </NavLink>
          ))}
        </nav>

        <div className="m-drawer-section">
          {pickerOpen ? (
            <>
              <div className="m-drawer-section-head">
                <span>选择角色</span>
                <button className="m-icon-btn" onClick={() => setPickerOpen(false)} aria-label="返回对话列表">
                  ‹
                </button>
              </div>
              {error && <div className="banner err">{error}</div>}
              {roles.length === 0 ? (
                <div className="banner info" style={{ margin: '0 12px' }}>
                  还没有角色。角色 = 人格设定 + 绑定的智能体，请先到「角色」页创建。
                </div>
              ) : (
                <div className="m-drawer-scroll">
                  <div className="m-list">
                    {roles.map((role) => (
                      <button
                        key={role.id}
                        className="m-item"
                        disabled={creating}
                        onClick={() => void start(role.id)}
                      >
                        <span className="avatar">{role.avatar}</span>
                        <span className="m-item-main">
                          <span className="m-item-title">{role.name}</span>
                          <span className="m-item-sub">
                            {role.agent_name} · {role.tagline ?? '无备注'}
                          </span>
                        </span>
                        <span className="pill brand">开始</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            <>
              <div className="m-drawer-section-head">
                <span>对话</span>
                <button
                  className="btn sm primary"
                  onClick={() => {
                    setError(null)
                    setPickerOpen(true)
                  }}
                >
                  ＋ 新建
                </button>
              </div>
              {error && <div className="banner err">{error}</div>}
              <div className="m-drawer-scroll">
                {loading ? (
                  <p className="dim small">正在加载对话…</p>
                ) : conversations.length === 0 ? (
                  <p className="dim small">还没有对话，点「＋ 新建」选一个角色开始。</p>
                ) : (
                  <div className="m-list">
                    {conversations.map((conversation) => (
                      // 一行里两个按钮（打开对话 / 会话操作），button 不能嵌套，所以拆成两段。
                      <div
                        key={conversation.id}
                        className={`m-item m-conv-row${conversation.id === activeId ? ' active' : ''}`}
                      >
                        <button
                          className="m-conv-main"
                          onClick={() => {
                            onClose()
                            navigate(`/chat/${conversation.id}`)
                          }}
                        >
                          <span className="m-item-main">
                            <span className="m-item-title">{conversation.title}</span>
                            <span className="m-item-sub m-item-sub-row">
                              <span className="m-item-meta">
                                {conversation.role_name} · {conversation.message_count} 条
                              </span>
                              <span className="m-item-time">{formatListTime(conversation.updated_at)}</span>
                            </span>
                          </span>
                        </button>
                        <button
                          className="m-conv-more"
                          aria-label={`对话「${conversation.title}」的操作`}
                          title="重新生成 / 重置记忆 / 复制工作目录 / 删除"
                          onClick={() => {
                            onClose()
                            navigate(`/chat/${conversation.id}`, { state: { openActions: true } })
                          }}
                        >
                          ⋯
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </aside>
    </div>
  )
}
