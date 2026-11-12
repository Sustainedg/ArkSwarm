/**
 * 内核 Logo：把一个单色 SVG 当作「遮罩」染成当前文字色。
 *
 * 用遮罩而不是 `<img>`：同一个图形要在深色 / 浅色两套外观下都清楚，遮罩染色会跟着
 * 文字色自动切换，不必为每个内核准备两张图。图形文件只画形状、不指定颜色
 * （`frontend/public/icons/kernels/*.svg`），路径由后端按适配器声明下发。
 */
export function KernelLogo({ logo }: { logo?: string | null }) {
  if (!logo) return null
  return (
    <span
      className="kernel-logo"
      aria-hidden="true"
      style={{ WebkitMaskImage: `url(${logo})`, maskImage: `url(${logo})` }}
    />
  )
}
