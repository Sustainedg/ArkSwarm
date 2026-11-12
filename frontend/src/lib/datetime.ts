/**
 * 列表里的时间文案：今天只显示时间，其余显示「日期 + 时间」。
 *
 * 对话列表（桌面端 `pages/ChatPage` 与移动端对话列表弹层）共用这一份，
 * 保证两端口径一致：今天 → `14:23`；今年 → `11-09 14:23`；跨年 → `2024-11-09 14:23`。
 */
export function formatListTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  if (isSameDay(date, now)) return time
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  return date.getFullYear() === now.getFullYear() ? `${day} ${time}` : `${date.getFullYear()}-${day} ${time}`
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

function isSameDay(left: Date, right: Date): boolean {
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  )
}
