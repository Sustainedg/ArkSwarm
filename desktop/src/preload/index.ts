/**
 * preload：桌面版对渲染层暴露的极小 API 面。
 *
 * 渲染层（ArkSwarm 前端本身）不需要任何 Node 能力；
 * 这个 preload 只给启动页/托盘相关的少量桌面能力留口子，M1 阶段可为空实现。
 */
import { contextBridge } from 'electron';

contextBridge.exposeInMainWorld('arkswarmDesktop', {
  /** 桌面版标记（前端可据此显示托盘提示等）。 */
  isDesktop: true,
});
