/**
 * 品牌标：直接使用用户提供的品牌图（`/icons/icon-512.png`，与应用图标是同一张）。
 *
 * 不再内联绘制图形 —— 品牌长什么样以母版位图（`assets/brand/logo.png`）为准，
 * 避免「脚本画出来的和用户给的不一样」。母版是位图，所以用 `<img>` 而不是内联 SVG；
 * 它自带品牌色，深浅两套外观下都按原色显示（品牌图本身就是确定的颜色）。
 */
export function BrandMark() {
  return (
    <img
      className="brand-mark-glyph"
      src="/icons/icon-512.png"
      alt=""
      aria-hidden="true"
      draggable={false}
    />
  )
}
