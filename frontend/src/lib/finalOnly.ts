/**
 * 「显示工具调用」开关（输入区齿轮菜单里那一项）：关掉后对话只亮最终回复 ——
 * 推理 / 工具调用 / 中间解说词全部藏起来，运行中只显示思考气泡，
 * 跑完只亮时间线上最后一段正文。
 *
 * 存储语义与朗读相反：**默认显示**（没设置过就是完整视图），只有显式关过才藏 ——
 * 存的是「关掉」这个动作（值 `'off'`），删掉键 = 回到默认的完整视图。
 *
 * 与 `lib/tts.ts`、`lib/theme.ts` 同属「这台设备的个人偏好」：不上传、不落库，
 * 换台设备各管各的。存储键带 `coda` 前缀。
 */
import { useCallback, useState } from 'react'

/** 存储键（值只写 `'off'`；没这个键 = 默认显示工具调用）。 */
export const TOOLS_VISIBILITY_STORAGE_KEY = 'coda.tools-hidden'

/** 读开关；没设置过就是显示（默认完整视图）。 */
export function readShowTools(): boolean {
  try {
    return localStorage.getItem(TOOLS_VISIBILITY_STORAGE_KEY) !== 'off'
  } catch {
    /* 隐私模式下 localStorage 可能不可用：按默认显示处理 */
    return true
  }
}

/** 写开关；回到显示时把键删掉，保持「默认显示」只有一个真值来源。 */
export function storeShowTools(show: boolean): void {
  try {
    if (show) localStorage.removeItem(TOOLS_VISIBILITY_STORAGE_KEY)
    else localStorage.setItem(TOOLS_VISIBILITY_STORAGE_KEY, 'off')
  } catch {
    /* 写不进去就只在本次会话里生效 */
  }
}

/**
 * 对话页用的「显示工具调用」开关：`showTools` 是当前值（默认显示），
 * `setShowTools` 写状态的同时落 localStorage —— 刷新 / 重开页面后偏好还在。
 * 传给 `Composer` 的 `showTools` / `onShowToolsChange`；`MessageView` 那头
 * 取反就是 `finalOnly`（关掉显示 = 纯净视图）。
 */
export function useShowTools(): { showTools: boolean; setShowTools: (show: boolean) => void } {
  const [showTools, setShowToolsState] = useState(readShowTools)
  const setShowTools = useCallback((value: boolean) => {
    setShowToolsState(value)
    storeShowTools(value)
  }, [])
  return { showTools, setShowTools }
}
