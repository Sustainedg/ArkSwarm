/**
 * 路径展示的小工具（两端共用）。
 *
 * 只留最后一段：输入框上方那排开关很窄，塞不进 `/vol1/1000/同步库/…/my-app` 这么长的路径，
 * 完整路径仍然挂在按钮的 `title` 上（桌面悬停 / 手机长按都能看到）。
 */
export function shortPath(value: string | null | undefined, fallback = '默认目录'): string {
  if (!value) return fallback
  const parts = value.replace(/\/+$/, '').split('/')
  return parts[parts.length - 1] || value
}
