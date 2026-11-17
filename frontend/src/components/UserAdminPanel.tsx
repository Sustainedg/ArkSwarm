import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { saveSession } from '../lib/auth'
import { useAuth } from './AuthGate'
import { UserAvatar } from './UserAvatar'
import type { AuthUser } from '../lib/types'

/**
 * 用户管理（仅管理员）：新建成员、改角色 / 启用状态、重置口令。
 *
 * 桌面端在「设置 → 用户」，移动端在「账号 → 用户管理」。两端共用这一份。
 */
export function UserAdminPanel() {
  const { user: me } = useAuth()
  const [items, setItems] = useState<AuthUser[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const [resetFor, setResetFor] = useState<string | null>(null)
  const [resetValue, setResetValue] = useState('')
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setItems(await api.listUsers())
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '读取用户失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const create = async (event: React.FormEvent) => {
    event.preventDefault()
    setCreating(true)
    setCreateError(null)
    setNotice(null)
    try {
      await api.createUser({
        username: username.trim(),
        password,
        display_name: displayName.trim() || undefined,
      })
      setUsername('')
      setDisplayName('')
      setPassword('')
      setNotice('用户已创建')
      await load()
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.message : '创建失败')
    } finally {
      setCreating(false)
    }
  }

  const patch = async (row: AuthUser, body: { role?: string; is_active?: boolean }) => {
    setBusyId(row.id)
    setError(null)
    setNotice(null)
    try {
      const next = await api.updateUser(row.id, body)
      setItems((prev) => prev.map((item) => (item.id === next.id ? next : item)))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '保存失败')
    } finally {
      setBusyId(null)
    }
  }

  const reset = async (row: AuthUser) => {
    setBusyId(row.id)
    setError(null)
    setNotice(null)
    try {
      const result = await api.resetUserPassword(row.id, resetValue)
      if (me && row.id === me.id && result.access_token) {
        // 管理员重置的是自己：换用新令牌，避免下一次请求被登出。
        saveSession(result.access_token, me)
      }
      setResetFor(null)
      setResetValue('')
      setNotice(`已重置「${row.display_name || row.username}」的口令`)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '重置失败')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <>
      <section className="card account-card">
        <h2 className="card-title">新建用户</h2>
        <p className="dim small">管理员创建的用户默认是「成员」，创建后可在这里改角色。</p>
        <form className="stack" onSubmit={create}>
          <div className="grid three">
            <label className="field">
              <span className="field-label">用户名</span>
              <input
                className="input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoCapitalize="none"
                spellCheck={false}
                minLength={2}
                maxLength={60}
                required
              />
            </label>
            <label className="field">
              <span className="field-label">显示名（可选）</span>
              <input
                className="input"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                maxLength={120}
              />
            </label>
            <label className="field">
              <span className="field-label">初始口令</span>
              <input
                className="input"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={8}
                required
              />
            </label>
          </div>
          {createError && <div className="banner err">{createError}</div>}
          {notice && <div className="banner ok">{notice}</div>}
          <div className="row">
            <button className="btn primary" type="submit" disabled={creating}>
              {creating ? '创建中…' : '创建用户'}
            </button>
          </div>
        </form>
      </section>

      <section className="card account-card">
        <div className="card-head">
          <h2 className="card-title">全部用户</h2>
          <button className="btn sm" onClick={() => void load()} disabled={loading}>
            ↻ 刷新
          </button>
        </div>
        {error && <div className="banner err">{error}</div>}
        {loading && items.length === 0 ? (
          <p className="dim small">正在读取用户…</p>
        ) : (
          <div className="stack" style={{ gap: 8 }}>
            {items.map((row) => {
              const self = row.id === me?.id
              return (
                <div className="account-user-row" key={row.id}>
                  <UserAvatar user={row} size={34} />
                  <div className="account-user-main">
                    <div className="account-user-name">
                      {row.display_name || row.username}
                      {self && <span className="pill" style={{ marginLeft: 6 }}>你</span>}
                      {!row.is_active && <span className="pill err" style={{ marginLeft: 6 }}>已禁用</span>}
                    </div>
                    <div className="dim small">
                      <span className="mono-sm">{row.username}</span>
                      {row.email ? ` · ${row.email}` : ''}
                    </div>
                  </div>
                  <div className="account-user-actions">
                    <select
                      className="select sm"
                      value={row.role}
                      disabled={busyId === row.id || self}
                      onChange={(e) => void patch(row, { role: e.target.value })}
                    >
                      <option value="member">成员</option>
                      <option value="admin">管理员</option>
                    </select>
                    <button
                      className="btn sm"
                      disabled={busyId === row.id || self}
                      onClick={() => void patch(row, { is_active: !row.is_active })}
                    >
                      {row.is_active ? '禁用' : '启用'}
                    </button>
                    <button
                      className="btn sm"
                      disabled={busyId === row.id}
                      onClick={() => {
                        setResetFor(resetFor === row.id ? null : row.id)
                        setResetValue('')
                      }}
                    >
                      重置口令
                    </button>
                  </div>
                  {resetFor === row.id && (
                    <div className="account-reset-row">
                      <input
                        className="input"
                        type="password"
                        placeholder="新口令（至少 8 个字符）"
                        value={resetValue}
                        onChange={(e) => setResetValue(e.target.value)}
                        minLength={8}
                      />
                      <button
                        className="btn sm primary"
                        disabled={resetValue.length < 8 || busyId === row.id}
                        onClick={() => void reset(row)}
                      >
                        确认重置
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </section>
    </>
  )
}
