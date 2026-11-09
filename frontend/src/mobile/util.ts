/**
 * 移动端共用的小工具：错误文案与时间显示。
 * 这里只放纯函数，不碰 DOM、不发请求。
 */
import { ApiError } from '../lib/api'

/** 把任意异常转成可直接展示的中文提示。 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}

/** 把 ISO 时间转成「刚刚 / 12 分钟前 / 3 小时前 / 11月9日」这类手机上好读的文案。 */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return ''
  const then = new Date(iso)
  const value = then.getTime()
  if (Number.isNaN(value)) return ''
  const diff = Date.now() - value
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`
  return `${then.getMonth() + 1}月${then.getDate()}日`
}

/** 秒数转「1分12秒」。 */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return `${total}秒`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return rest ? `${minutes}分${rest}秒` : `${minutes}分`
}

/** 截断过长的字符串（避免把密钥、长路径原样铺满屏幕）。 */
export function short(value: string | null | undefined, max = 28): string {
  if (!value) return ''
  return value.length > max ? `${value.slice(0, max)}…` : value
}
