import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { saveSession } from '../lib/auth'
import type { AuthConfig, AuthSession } from '../lib/types'
import { BrandMark } from './BrandMark'

interface Props {
  config: AuthConfig
  /** 登录 / 注册成功（令牌已写入本机）后交给 AuthGate 切换界面 */
  onAuthed: (session: AuthSession) => void
  /** 移动端：同样的表单换一套更紧凑的排版（样式在 mobile.css） */
  mobile?: boolean
}

/**
 * 登录 / 注册表单。
 *
 * 两端共用这一份（桌面端与移动端只是外壳不同）：字段、错误提示与「首任注册者自动成为
 * 管理员」的说明必须完全一致，避免两处各写一遍后慢慢跑偏。
 */
export function LoginScreen({ config, onAuthed, mobile }: Props) {
  const [mode, setMode] = useState<'login' | 'register'>(
    config.has_users ? 'login' : config.registration_open ? 'register' : 'login',
  )
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const canRegister = config.registration_open
  const firstUser = !config.has_users && config.registration_open

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const session =
        mode === 'register'
          ? await api.register({
              username: username.trim(),
              password,
              display_name: displayName.trim() || undefined,
            })
          : await api.login({ username: username.trim(), password })
      saveSession(session.access_token, session.user)
      onAuthed(session)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '登录失败，请稍后再试')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`auth-screen${mobile ? ' auth-mobile' : ''}`}>
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-mark">
            <BrandMark size={24} />
          </div>
          <div>
            <div className="auth-title">ArkSwarm</div>
            <div className="auth-sub">多智能体控制台</div>
          </div>
        </div>

        <div className="auth-heading">
          <h1>{mode === 'login' ? '登录' : '创建账号'}</h1>
          <p className="dim small">
            {firstUser
              ? '这是第一个账号，注册后会自动成为管理员。'
              : mode === 'login'
                ? '用你的账号登录，继续管理智能体与对话。'
                : '注册后即可创建自己的智能体、网关与对话。'}
          </p>
        </div>

        <form className="stack auth-form" onSubmit={submit}>
          <label className="field">
            <span className="field-label">用户名</span>
            <input
              className="input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              required
              minLength={2}
              maxLength={60}
              autoFocus
            />
          </label>
          {mode === 'register' && (
            <label className="field">
              <span className="field-label">显示名（可选）</span>
              <input
                className="input"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                autoComplete="nickname"
                maxLength={120}
              />
            </label>
          )}
          <label className="field">
            <span className="field-label">口令</span>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              required
              minLength={mode === 'register' ? 8 : 1}
              maxLength={200}
            />
            {mode === 'register' && <span className="field-help">至少 8 个字符。</span>}
          </label>

          {error && <div className="banner err">{error}</div>}

          <button className="btn primary auth-submit" type="submit" disabled={busy}>
            {busy ? '请稍候…' : mode === 'login' ? '登录' : '注册并登录'}
          </button>
        </form>

        {canRegister && (
          <div className="auth-switch">
            {mode === 'login' ? (
              <button type="button" className="link-btn" onClick={() => { setMode('register'); setError(null) }}>
                还没有账号？创建一个
              </button>
            ) : (
              <button type="button" className="link-btn" onClick={() => { setMode('login'); setError(null) }}>
                已经有账号？直接登录
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
