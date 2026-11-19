/**
 * 「更新设置」：自动检查更新的开关与每日检查时刻（设置 → 关于 → 更新设置弹窗）。
 *
 * 开启后每天在设定的小时自动打一次 GitHub 检测（由后端定时任务执行，这里只是
 * 界面偏好）。与 `lib/tts.ts`、`lib/theme.ts` 同属「这台设备的个人偏好」：不上传、
 * 不落库，换台设备各管各的。
 *
 * 存储：键 `arkswarm.auto-check-updates` 存检查时刻（`'9'` = 每日 9 点；没这个键
 * 就是关闭）。时刻与开关合成一个真值 —— 「设了时刻」=「开着」，「删键」=「关了」，
 * 不存在「开着但没时刻」的中间态。
 */
import { useCallback, useState } from 'react'

/** 存储键（值是检查时刻的小时数；没这个键就是关闭）。 */
export const AUTO_CHECK_UPDATES_KEY = 'arkswarm.auto-check-updates'

/** 默认检查时刻：早上 9 点（上班开机后不久，还没开始忙）。 */
export const DEFAULT_CHECK_HOUR = 9

/** 读偏好；没设置过就是关闭（null）。开着时返回每日检查的小时（0-23）。 */
export function readAutoCheckHour(): number | null {
  try {
    const raw = localStorage.getItem(AUTO_CHECK_UPDATES_KEY)
    if (raw === null) return null
    const hour = Number.parseInt(raw, 10)
    return Number.isInteger(hour) && hour >= 0 && hour <= 23 ? hour : null
  } catch {
    /* 隐私模式下 localStorage 可能不可用：按关闭处理 */
    return null
  }
}

/** 写偏好：给小时（0-23）= 开启；给 null = 关闭（删键）。 */
export function storeAutoCheckHour(hour: number | null): void {
  try {
    if (hour === null) localStorage.removeItem(AUTO_CHECK_UPDATES_KEY)
    else localStorage.setItem(AUTO_CHECK_UPDATES_KEY, String(hour))
  } catch {
    /* 写不进去就只在本次会话里生效 */
  }
}

/**
 * 版本更新面板用的偏好：`hour` 当前检查时刻（null = 关闭），
 * `setHour` 落 localStorage。弹窗里的开关只是「hour 有没有值」的另一种表现。
 */
export function useAutoCheckUpdates(): { hour: number | null; setHour: (hour: number | null) => void } {
  const [hour, setHourState] = useState(readAutoCheckHour)
  const setHour = useCallback((value: number | null) => {
    setHourState(value)
    storeAutoCheckHour(value)
  }, [])
  return { hour, setHour }
}
