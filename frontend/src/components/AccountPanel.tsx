import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { saveSession, saveUser } from '../lib/auth'
import { useAuth } from './AuthGate'

/**
 * 个人资料与口令。
 *
 * 两端共用：桌面端在「设置 → 账号」，移动端是独立的「账号」页。鉴权关闭的部署里
 * 当前用户是虚拟管理员，这里只显示说明，不提供改口令 / 退出。
 */
export function AccountPanel() {
  const { user, authEnabled, logout, refreshUser } = useAuth()
  const [displayName, setDisplayName] = useState(user?.display_name ?? '')
  const [email, setEmail] = useState(user?.email ?? '')
  const [profileBusy, setProfileBusy] = useState(false)
  const [profileMessage, setProfileMessage] = useState<string | null>(null)
  const [profileError, setProfileError] = useState<string | null>(null)

  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [passwordBusy, setPasswordBusy] = useState(false)
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null)
  const [passwordError, setPasswordError] = useState<string | null>(null)

  useEffect(() => {
    setDisplayName(user?.display_name ?? '')
    setEmail(user?.email ?? '')
  }, [user?.display_name, user?.email])

  if (!authEnabled || !user || user.id === '__system__') {
    return (
      <section className="card account-card">
        <div className="card-title">单用户模式</div>
        <p className="dim small">
          当前部署关闭了鉴权（<span className="mono-sm">ARKSWARM_AUTH_ENABLED=false</span>），
          所有请求都按管理员处理，没有需要维护的账号。
        </p>
      </section>
    )
  }

  const saveProfile = async (event: React.FormEvent) => {
    event.preventDefault()
    setProfileBusy(true)
    setProfileError(null)
    setProfileMessage(null)
    try {
      const next = await api.updateMe({ display_name: displayName.trim(), email: email.trim() })
      saveUser(next)
      refreshUser(next)
      setProfileMessage('已保存')
    } catch (e) {
      setProfileError(e instanceof ApiError ? e.message : '保存失败')
    } finally {
      setProfileBusy(false)
    }
  }

  const changePassword = async (event: React.FormEvent) => {
    event.preventDefault()
    setPasswordBusy(true)
    setPasswordError(null)
    setPasswordMessage(null)
    try {
      const result = await api.changePassword({ old_password: oldPassword, new_password: newPassword })
      // 后端会递增 token_version 让旧令牌失效；这里保存它新签发的令牌，避免下一次请求被登出。
      saveSession(result.access_token, user)
      setOldPassword('')
      setNewPassword('')
      setPasswordMessage('口令已更新，下次登录请用新口令')
    } catch (e) {
      setPasswordError(e instanceof ApiError ? e.message : '修改失败')
    } finally {
      setPasswordBusy(false)
    }
  }

  return (
    <>
      <section className="card account-card">
        <div className="card-head">
          <div>
            <h2 className="card-title">
              {user.display_name || user.username}
              <span className={`pill ${user.role === 'admin' ? 'ok' : ''}`} style={{ marginLeft: 8 }}>
                {user.role === 'admin' ? '管理员' : '成员'}
              </span>
            </h2>
            <div className="card-sub">
              登录名 <span className="mono-sm">{user.username}</span>
            </div>
          </div>
          <button className="btn sm" onClick={logout}>
            退出登录
          </button>
        </div>

        <form className="stack" onSubmit={saveProfile}>
          <div className="grid two">
            <label className="field">
              <span className="field-label">显示名</span>
              <input
                className="input"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                maxLength={120}
                placeholder={user.username}
              />
            </label>
            <label className="field">
              <span className="field-label">邮箱（可选）</span>
              <input
                className="input"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                maxLength={200}
              />
            </label>
          </div>
          {profileError && <div className="banner err">{profileError}</div>}
          {profileMessage && <div className="banner ok">{profileMessage}</div>}
          <div className="row">
            <button className="btn primary" type="submit" disabled={profileBusy}>
              {profileBusy ? '保存中…' : '保存资料'}
            </button>
          </div>
        </form>
      </section>

      <section className="card account-card">
        <h2 className="card-title">修改口令</h2>
        <form className="stack" onSubmit={changePassword}>
          <div className="grid two">
            <label className="field">
              <span className="field-label">当前口令</span>
              <input
                className="input"
                type="password"
                autoComplete="current-password"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                required
              />
            </label>
            <label className="field">
              <span className="field-label">新口令</span>
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                minLength={8}
                required
              />
              <span className="field-help">至少 8 个字符。</span>
            </label>
          </div>
          {passwordError && <div className="banner err">{passwordError}</div>}
          {passwordMessage && <div className="banner ok">{passwordMessage}</div>}
          <div className="row">
            <button className="btn" type="submit" disabled={passwordBusy}>
              {passwordBusy ? '更新中…' : '更新口令'}
            </button>
          </div>
        </form>
      </section>
    </>
  )
}
