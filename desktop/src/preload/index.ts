/**
 * preload：桌面版对渲染层暴露的极小 API 面。
 *
 * 渲染层（ArkSwarm 前端本身）不需要任何 Node 能力；这里只给三类白名单能力：
 *   1. 「自绘标题栏」的三个窗口动作（最小化 / 最大化切换 / 关闭）；
 *   2. 桌面壳自身的更新（与托盘菜单同一套 electron-updater 状态机）；
 *   3. 桌面版标记。
 * 关闭走 win.close()，主进程的 close 事件会把它隐藏到托盘（后端继续跑）。
 */
import { contextBridge, ipcRenderer } from 'electron';

type WindowAction = 'minimize' | 'toggle-maximize' | 'close';

/** 与 `main/updater.ts` 的 DesktopUpdateState 保持一致（preload 不能 import 主进程模块）。 */
interface DesktopUpdateState {
  supported: boolean;
  current_version: string;
  phase: 'idle' | 'checking' | 'downloading' | 'downloaded' | 'up-to-date' | 'error' | 'unsupported';
  available_version: string | null;
  percent: number;
  error: string | null;
}

contextBridge.exposeInMainWorld('arkswarmDesktop', {
  /** 桌面版标记（前端可据此切到桌面专属的更新面板）。 */
  isDesktop: true,
  window: {
    run: (action: WindowAction) => {
      // 白名单：渲染层只能触发这三个动作，不接受任意 IPC 通道
      if (action === 'minimize' || action === 'toggle-maximize' || action === 'close') {
        ipcRenderer.send('desktop-window', action);
      }
    },
    onMaximized: (callback: (maximized: boolean) => void) => {
      ipcRenderer.on('desktop-window-state', (_event, maximized: boolean) => {
        callback(Boolean(maximized));
      });
    },
  },
  updates: {
    /** 当前更新状态快照（首屏用；之后靠 onState 推送）。 */
    status: (): Promise<DesktopUpdateState> => ipcRenderer.invoke('desktop-update:status'),
    /** 检查更新：与托盘「检查桌面端更新…」同一个 electron-updater 通道。 */
    check: (): Promise<DesktopUpdateState> => ipcRenderer.invoke('desktop-update:check'),
    /** 下载完成后重启安装（等价于托盘弹窗里的「立即重启安装」）。 */
    install: (): Promise<void> => ipcRenderer.invoke('desktop-update:install'),
    /** 订阅状态变化；返回取消订阅函数。 */
    onState: (callback: (state: DesktopUpdateState) => void): (() => void) => {
      const listener = (_event: Electron.IpcRendererEvent, state: DesktopUpdateState): void => {
        callback(state);
      };
      ipcRenderer.on('desktop-update-state', listener);
      return () => ipcRenderer.removeListener('desktop-update-state', listener);
    },
  },
});