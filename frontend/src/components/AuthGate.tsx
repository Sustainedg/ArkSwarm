import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError } from '../lib/api'
import { clearSession, getAccessToken, getStoredUser, UNAUTHORIZED_EVENT } from '../lib/auth'
import type { AuthConfig, AuthSession, AuthUser } from '../lib/types'
import { LoginScreen } from './LoginScreen'

interface AuthState {
  /** 当前用户；未登录时为 null */
  user: AuthUser | null
  /** 后端是否开启鉴权：关闭时 user 是虚拟管理员，界面不显示账号入口 */
  authEnabled: boolean
  isAdmin: boolean
  logout: () => void
  /** 用户资料更新后同步侧边栏 / 抽屉里的名字 */
  refreshUser: (user: AuthUser) => void
}

const AuthContext = createContext<AuthState | null>(null)

/** 页面里读当前登录用户（侧边栏、设置页的账号入口）。必须包在 AuthGate 里。 */
export function useAuth(): AuthState {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth 必须在 AuthGate 内使用')
  return value
}

/**
 * 登录门：首屏先问后端「我是谁」，未登录就显示登录 / 注册页。
 *
 * 鉴权关闭的部署（`CODA_AUTH_ENABLED=false`）也能正常进入：后端会把每个请求
 * 当虚拟管理员，这里拿到的 `user.id === '__system__'`，只是不显示账号相关入口。
 */
export function AuthGate({ children, mobile }: { children: ReactNode; mobile?: boolean }) {
  const [status, setStatus] = useState<'loading' | 'anon' | 'ready' | 'error'>('loading')
  const [config, setConfig] = useState<AuthConfig | null>(null)
  const [user, setUser] = useState<AuthUser | null>(getStoredUser())
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setStatus('loading')
    setError(null)
    try {
      const cfg = await api.authConfig()
      setConfig(cfg)
      if (!getAccessToken()) {
        // 鉴权关闭时 /me 不要求令牌；先试一次，免得这种部署也被拦在登录页。
        if (!cfg.auth_enabled) {
          const me = await api.me()
          setUser(me)
          setStatus('ready')
          return
        }
        setUser(null)
        setStatus('anon')
        return
      }
      const me = await api.me()
      setUser(me)
      setStatus('ready')
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        clearSession()
        setUser(null)
        setStatus('anon')
      } else {
        setError(e instanceof Error ? e.message : '无法连接服务端')
        setStatus('error')
      }
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    const onUnauthorized = () => {
      clearSession()
      setUser(null)
      setStatus('anon')
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [])

  const onAuthed = (session: AuthSession) => {
    setUser(session.user)
    // 刚注册完（或首次登录）库里就有用户了：退出后登录页应默认显示「登录」而不是「注册」。
    setConfig((prev) => (prev ? { ...prev, has_users: true } : prev))
    setStatus('ready')
  }

  const logout = useCallback(() => {
    clearSession()
    setUser(null)
    setStatus('anon')
  }, [])

  const refreshUser = useCallback((next: AuthUser) => setUser(next), [])

  const value = useMemo<AuthState>(
    () => ({
      user,
      authEnabled: config?.auth_enabled ?? true,
      isAdmin: user?.role === 'admin',
      logout,
      refreshUser,
    }),
    [user, config, logout, refreshUser],
  )

  if (status === 'loading') {
    return (
      <div className={`auth-screen${mobile ? ' auth-mobile' : ''}`}>
        <div className="auth-loading">正在连接 Coda…</div>
      </div>
    )
  }

  if (status === 'error') {
    return (
      <div className={`auth-screen${mobile ? ' auth-mobile' : ''}`}>
        <div className="auth-card">
          <div className="auth-heading">
            <h1>无法连接服务端</h1>
            <p className="dim small">{error}</p>
          </div>
          <button className="btn primary" onClick={() => void load()}>
            重试
          </button>
        </div>
      </div>
    )
  }

  if (status === 'anon' || !user) {
    return (
      <LoginScreen
        config={config ?? { auth_enabled: true, registration_open: true, has_users: false }}
        onAuthed={onAuthed}
        mobile={mobile}
      />
    )
  }

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
