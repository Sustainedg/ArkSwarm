/**
 * 内核 Logo：两种形态，由后端按适配器声明下发。
 *
 * - `mask`（默认）：把一个单色 SVG 当作「遮罩」染成当前文字色。同一个图形要在深色 / 浅色
 *   两套外观下都清楚，遮罩染色会跟着文字色自动切换，不必为每个内核准备两张图。
 * - `image`：真彩色位图（PNG / WebP）或彩色 SVG，按原色显示 —— 品牌图标本来就该是
 *   「实打实」的样子，染成单色反而认不出来。
 *
 * 图形文件只画形状、不指定颜色（`frontend/public/icons/kernels/*.svg`），路径由后端按
 * 适配器声明下发；位图同样随仓库发布，运行时不外链。
 */
import { useEffect, useState } from 'react'

/** 位图扩展名：没有显式声明 logo_style 时按扩展名推断。 */
const RASTER = /\.(png|webp|jpe?g)$/i

export function KernelLogo({
  logo,
  style,
}: {
  logo?: string | null
  /** 后端下发的 logo_style；缺省时按扩展名推断 */
  style?: 'mask' | 'image' | null
}) {
  const [broken, setBroken] = useState(false)
  // 换了图重置裂图标记：上一次加载失败不代表这一次也失败。
  useEffect(() => setBroken(false), [logo])
  if (!logo) return null

  const image = (style ?? (RASTER.test(logo) ? 'image' : 'mask')) === 'image'
  if (!image) {
    return (
      <span
        className="kernel-logo"
        aria-hidden="true"
        style={{ WebkitMaskImage: `url(${logo})`, maskImage: `url(${logo})` }}
      />
    )
  }
  if (broken) return null
  return (
    <img
      className="kernel-logo kernel-logo-image"
      src={logo}
      alt=""
      loading="lazy"
      onError={() => setBroken(true)}
    />
  )
}
