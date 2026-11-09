/**
 * 移动端 PWA 支持：Service Worker 注册与「添加到主屏」引导。
 *
 * Service Worker 只在生产构建里注册（开发环境下缓存会干扰热更新），
 * 并且只接管 `/m/` 这一条作用域；接口 `/api`（含 SSE）永远直连后端。
 */
import { useCallback, useEffect, useState } from 'react'

/** 离线缓存的前缀（与 `public/m/sw.js` 里的缓存名保持一致）。 */
const CACHE_PREFIX = 'magenta-m-'

/** Chromium 专有的安装事件（TS 标准库里没有）。 */
interface InstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

/** 注册 /m/ 的 Service Worker（同源、幂等；重复注册由浏览器自行合并）。 */
export function registerServiceWorker(): void {
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/m/sw.js', { scope: '/m/' }).catch((error: unknown) => {
      console.warn('移动端 Service Worker 注册失败：', error)
    })
  })
}

/**
 * 强制取回最新的应用外壳：注销 Service Worker、清掉离线缓存，然后重新加载。
 *
 * 独立应用（已添加到主屏）里没有地址栏也没有刷新按钮，界面卡在旧外壳时只能靠它自救；
 * 清完重载后入口脚本会重新注册 SW 并预缓存当前版本。
 */
export async function refreshShell(): Promise<void> {
  try {
    if ('serviceWorker' in navigator) {
      const registrations = await navigator.serviceWorker.getRegistrations()
      await Promise.all(registrations.map((registration) => registration.unregister()))
    }
    if ('caches' in window) {
      const keys = await caches.keys()
      await Promise.all(keys.filter((key) => key.startsWith(CACHE_PREFIX)).map((key) => caches.delete(key)))
    }
  } catch (error: unknown) {
    console.warn('清理离线缓存失败，仍然重新加载：', error)
  }
  window.location.reload()
}

/** 是否已经以「安装后的独立应用」形态运行（而不是浏览器标签页）。 */
export function isStandalone(): boolean {
  const iosStandalone = (navigator as Navigator & { standalone?: boolean }).standalone === true
  return window.matchMedia?.('(display-mode: standalone)').matches === true || iosStandalone
}

/** 是否 iOS/iPadOS：这些浏览器不支持 beforeinstallprompt，需要给出图文指引。 */
export function isIos(): boolean {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
}

export interface InstallController {
  /** 已是独立应用（无需再引导安装） */
  standalone: boolean
  /** 系统是 iOS，安装入口藏在「分享」里 */
  ios: boolean
  /** 浏览器已就绪，可弹出原生安装框（Chromium 系） */
  nativeReady: boolean
  /** 触发安装；返回 false 表示只能给图文指引 */
  install: () => Promise<boolean>
}

/** 把「添加到主屏」的可用性与触发方式封装成 hooks，界面只关心能不能装、怎么装。 */
export function useInstallController(): InstallController {
  const [prompt, setPrompt] = useState<InstallPromptEvent | null>(null)
  const [standalone, setStandalone] = useState(isStandalone())

  useEffect(() => {
    const onPrompt = (event: Event) => {
      event.preventDefault()
      setPrompt(event as InstallPromptEvent)
    }
    const onInstalled = () => {
      setPrompt(null)
      setStandalone(true)
    }
    window.addEventListener('beforeinstallprompt', onPrompt)
    window.addEventListener('appinstalled', onInstalled)
    const media = window.matchMedia?.('(display-mode: standalone)')
    const onChange = () => setStandalone(isStandalone())
    media?.addEventListener?.('change', onChange)
    return () => {
      window.removeEventListener('beforeinstallprompt', onPrompt)
      window.removeEventListener('appinstalled', onInstalled)
      media?.removeEventListener?.('change', onChange)
    }
  }, [])

  const install = useCallback(async () => {
    if (!prompt) return false
    await prompt.prompt()
    const choice = await prompt.userChoice
    if (choice.outcome !== 'dismissed') setStandalone(true)
    setPrompt(null)
    return true
  }, [prompt])

  return { standalone, ios: isIos(), nativeReady: prompt !== null, install }
}
