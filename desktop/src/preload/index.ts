/**
 * preload：桌面版对渲染层暴露的极小 API 面。
 *
 * 渲染层（ArkSwarm 前端本身）不需要任何 Node 能力；这里只给「自绘标题栏」
 * 提供三个窗口动作（最小化 / 最大化切换 / 关闭），以及最大化状态回传。
 * 关闭走 win.close()，主进程的 close 事件会把它隐藏到托盘（后端继续跑）。
 */
import { contextBridge, ipcRenderer } from 'electron';

type WindowAction = 'minimize' | 'toggle-maximize' | 'close';

contextBridge.exposeInMainWorld('arkswarmDesktop', {
  /** 桌面版标记（前端可据此显示托盘提示等）。 */
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
});
