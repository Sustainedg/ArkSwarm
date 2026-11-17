/**
 * 用户头像：取图、缓存、上传前的本地校验（两端共用）。
 *
 * 为什么不把 `/api/auth/users/{id}/avatar` 直接塞进 `<img src>`：那个接口要鉴权，
 * 而 `<img>` 带不了 `Authorization` 头；现有做法是把令牌拼进查询参数，但令牌会因此
 * 进访问日志与浏览器历史（见 `app/auth_deps.py` 的注释）。头像是「一屏里可能出现好几次」
 * 的资源，更不该走这条路。这里改成带令牌 `fetch` 成 blob，再用 object URL 渲染。
 *
 * 缓存键是 `用户 id + 头像标识`：换头像后标识变了，旧 object URL 自然被弃用。
 */
import { useEffect, useState } from 'react'
import { getAccessToken } from './auth'
import type { AuthUser } from './types'

const BASE = import.meta.env.VITE_API_BASE ?? ''

/** 单张头像上限，与后端 `app/avatars.py` 的 `MAX_AVATAR_BYTES` 保持一致。 */
export const MAX_AVATAR_BYTES = 2 * 1024 * 1024

/** 允许的 MIME：与后端只认的三种位图对齐。 */
const ALLOWED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp'])

/** 已取到的头像 object URL：`<id>:<avatar>` → URL。 */
const cache = new Map<string, string>()
/** 正在取的请求：同一张头像被多个组件同时要时只发一次。 */
const inflight = new Map<string, Promise<string | null>>()

function cacheKey(user: Pick<AuthUser, 'id' | 'avatar'>): string {
  return `${user.id}:${user.avatar ?? ''}`
}

async function loadAvatar(user: Pick<AuthUser, 'id' | 'avatar'>): Promise<string | null> {
  const key = cacheKey(user)
  const cached = cache.get(key)
  if (cached) return cached
  const pending = inflight.get(key)
  if (pending) return pending

  const task = (async () => {
    const token = getAccessToken()
    const response = await fetch(
      `${BASE}/api/auth/users/${encodeURIComponent(user.id)}/avatar`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    )
    if (!response.ok) return null
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    // 换头像后旧标识的图不会再被用到：顺手回收，别让缓存无限长大。
    const prefix = `${user.id}:`
    for (const [staleKey, staleUrl] of cache) {
      if (staleKey.startsWith(prefix) && staleKey !== key) {
        URL.revokeObjectURL(staleUrl)
        cache.delete(staleKey)
      }
    }
    cache.set(key, url)
    return url
  })()

  inflight.set(key, task)
  try {
    return await task
  } finally {
    inflight.delete(key)
  }
}

/**
 * 当前用户（或列表里任意用户）的头像地址；没有头像 / 取图失败时为 `null`，
 * 调用方回退到「显示名首字母」。
 */
export function useAvatarUrl(user: Pick<AuthUser, 'id' | 'avatar'> | null | undefined): string | null {
  const key = user ? cacheKey(user) : ''
  const [url, setUrl] = useState<string | null>(() => (key ? cache.get(key) ?? null : null))

  useEffect(() => {
    if (!user?.avatar) {
      setUrl(null)
      return
    }
    let alive = true
    void loadAvatar(user).then((next) => {
      if (alive) setUrl(next)
    })
    return () => {
      alive = false
    }
  }, [user, key])

  return url
}

/** 显示名首字母（中文取第一个字，英文取首字母大写）；空名回退到「我」。 */
export function avatarInitial(name: string | null | undefined): string {
  const text = (name ?? '').trim()
  if (!text) return '我'
  return text.slice(0, 1).toUpperCase()
}

/** 上传前的本地校验：只收位图、大小可控。返回给用户看的中文原因，通过时返回 `null`。 */
export function validateAvatarFile(file: File): string | null {
  if (!ALLOWED_TYPES.has(file.type)) return '只支持 PNG / JPEG / WebP 图片'
  if (file.size > MAX_AVATAR_BYTES) return '图片不能超过 2 MB'
  if (file.size === 0) return '图片内容为空'
  return null
}

/** 把文件读成 base64（不带 `data:` 前缀，后端只解 base64 本体）。 */
export function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const comma = result.indexOf(',')
      if (comma < 0) {
        reject(new Error('读取图片失败'))
        return
      }
      resolve(result.slice(comma + 1))
    }
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })
}
