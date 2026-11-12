/**
 * 移动端的图标：一律是**纯线条**的内联 SVG（`stroke="currentColor"`），不用 emoji。
 *
 * 为什么不用 emoji：各系统字体的 emoji 大小、粗细、颜色都不一样 —— 附件 📎 在 iOS 上尤其大，
 * 同一排图标永远对不齐。线条图标跟文字一个颜色（`currentColor`），深浅色主题自动跟随，
 * 尺寸也统一由 `size` 控制。
 */
import type { ReactNode } from 'react'

interface IconProps {
  /** 边长（px），默认 20 */
  size?: number
  className?: string
}

/** 所有线条图标的公共外壳：24 的坐标系、圆头圆角、描边跟随文字颜色。 */
function Icon({ size = 20, className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

/** 喇叭：关闭时只有喇叭本体，开启时多两道声波（不靠颜色也能看出状态）。 */
export function SpeakerIcon({ on = false, ...props }: IconProps & { on?: boolean }) {
  return (
    <Icon {...props}>
      <path d="M11 4.5 6.5 8.5H3.5v7h3L11 19.5z" />
      {on && (
        <>
          <path d="M15 9.2a4 4 0 0 1 0 5.6" />
          <path d="M17.8 6.6a8 8 0 0 1 0 10.8" />
        </>
      )}
    </Icon>
  )
}

/** 分享：一个方框 + 向上的箭头（把对话导出成长图）。 */
export function ShareIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 15.5V3.8" />
      <path d="m8.2 7.4 3.8-3.8 3.8 3.8" />
      <path d="M5.5 13.5v5.7a1.5 1.5 0 0 0 1.5 1.5h10a1.5 1.5 0 0 0 1.5-1.5v-5.7" />
    </Icon>
  )
}

/** 附件：回形针。 */
export function PaperclipIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20 11.5 12 19.5a4.9 4.9 0 0 1-7-7l8-8a3.2 3.2 0 0 1 4.6 4.6l-8 7.9a1.6 1.6 0 0 1-2.3-2.3l7.2-7.2" />
    </Icon>
  )
}

/** 麦克风：语音输入。 */
export function MicIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.2a2.9 2.9 0 0 1 2.9 2.9v5a2.9 2.9 0 0 1-5.8 0v-5A2.9 2.9 0 0 1 12 3.2z" />
      <path d="M5.4 11.2v1a6.6 6.6 0 0 0 13.2 0v-1" />
      <path d="M12 18.8V21" />
    </Icon>
  )
}

/** 发送：向上的箭头。 */
export function SendIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 19.5V5" />
      <path d="m6.6 10.4 5.4-5.4 5.4 5.4" />
    </Icon>
  )
}

/** 停止：实心小方块（运行中替代发送）。 */
export function StopIcon({ size = 20, className }: IconProps) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="7" y="7" width="10" height="10" rx="2.4" fill="currentColor" />
    </svg>
  )
}
