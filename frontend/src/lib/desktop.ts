/**
 * 桌面壳（Electron）注入的桥。浏览器里访问时 `window.arkswarmDesktop` 不存在，
 * 调用方据此走网页逻辑。
 *
 * 这里只声明「壳更新」这一块：桌面端的更新只有一种形态 —— 下载完整安装包。
 * 「设置 → 关于」不再走后端的 `arkswarm.tar.gz` 一键更新（那是 Docker / 网页部署的通道），
 * 而是与托盘「检查桌面端更新…」共用同一个 electron-updater 状态机。
 */
import type { DesktopUpdateBridge } from './types'

declare global {
  interface Window {
    arkswarmDesktop?: {
      isDesktop?: boolean
      updates?: DesktopUpdateBridge
      window?: {
        run?: (action: string) => void
        onMaximized?: (callback: (maximized: boolean) => void) => void
      }
    }
  }
}

/** 桌面壳更新桥；浏览器里返回 undefined（据此切到网页版更新面板）。 */
export function desktopUpdates(): DesktopUpdateBridge | undefined {
  return window.arkswarmDesktop?.updates
}