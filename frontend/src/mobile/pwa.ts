/**
 * 移动端 PWA 支持：Service Worker 注册与「添加到主屏」引导。
 *
 * Service Worker 只在生产构建里注册（开发环境下缓存会干扰热更新），
 * 并且只接管 `/m/` 这一条作用域；接口 `/api`（含 SSE）永远直连后端。
 */
import { useCallback, useEffect, useState } from 'react'

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
