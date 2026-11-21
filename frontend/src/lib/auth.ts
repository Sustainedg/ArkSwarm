/**
 * 登录态的本地存储与「令牌失效」广播。
 *
 * 令牌只存在 localStorage（键名带 `coda` 前缀，与主题等本机设置一致）；接口请求
 * 统一由 `lib/api.ts` 从 `getAccessToken()` 取 Bearer 头。这里不碰 React，避免
 * `api.ts` 与页面/上下文互相引用。
 */
import type { AuthUser } from './types'

const TOKEN_KEY = 'coda.auth.token'
const USER_KEY = 'coda.auth.user'

/** 令牌失效（401）时派发的事件名：AuthGate 听到后回到登录页。 */
export const UNAUTHORIZED_EVENT = 'coda:unauthorized'

/** 读 localStorage 时可能被隐私模式拦住：所有读写都兜底成「没有」。 */
function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch {
    /* 存不进去只影响下次刷新，不影响当前会话 */
  }
}

export function getAccessToken(): string | null {
  return readStorage(TOKEN_KEY)
}

export function getStoredUser(): AuthUser | null {
  const raw = readStorage(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as AuthUser
  } catch {
    return null
  }
}

/** 登录 / 注册成功后保存令牌与用户信息。 */
export function saveSession(token: string, user: AuthUser): void {
  writeStorage(TOKEN_KEY, token)
  writeStorage(USER_KEY, JSON.stringify(user))
}

/** 只更新用户资料（显示名 / 邮箱变化后，侧边栏与设置页不用重新登录）。 */
export function saveUser(user: AuthUser): void {
  writeStorage(USER_KEY, JSON.stringify(user))
}

export function clearSession(): void {
  writeStorage(TOKEN_KEY, null)
  writeStorage(USER_KEY, null)
}

/** 广播「令牌已失效」；AuthGate 监听后清状态并显示登录页。 */
export function notifyUnauthorized(): void {
  clearSession()
  try {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
  } catch {
    /* 非浏览器环境（测试 / SSR）没有 window */
  }
}
