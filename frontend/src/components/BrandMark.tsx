/**
 * 品牌标：三节点星座（一颗中枢 + 两颗成员 + 顶上的轨道弧）。
 *
 * 与应用图标（`scripts/gen_icons.py` 生成的 `public/icons/icon.svg`）同一套几何 ——
 * 图标是「渐变方块 + 白色星座」，这里是「当前文字色的星座」放在品牌底上，
 * 改图形时两边一起改。
 *
 * 为什么内联 SVG 而不是 `<img>`：界面里的品牌标要跟着主题换色（深色底上是浅色、
 * 浅色底上要压深），内联之后 `currentColor` 自动跟随，不必为两套外观各准备一张图。
 *
 * 小尺寸（≤22px）下只保留节点、收起连线与轨道弧：3 条 0.8px 的弧线缩到这个尺寸只会
 * 糊成一团，反而把「三个点」这个最容易记住的特征冲淡。
 */
export function BrandMark({ size = 20 }: { size?: number }) {
  const detailed = size > 22
  return (
    <svg
      className="brand-mark-glyph"
      width={size}
      height={size}
      viewBox="0 0 512 512"
      aria-hidden="true"
      focusable="false"
    >
      {detailed && (
        <>
          {/* 顶上的轨道弧：与中枢同心，两端垂到主节点两侧 */}
          <path
            d="M140.6 238.5 A118 118 0 1 1 371.4 238.5"
            fill="none"
            stroke="currentColor"
            strokeOpacity="0.5"
            strokeWidth="26"
            strokeLinecap="round"
          />
          {/* 三条连线：向质心内收的二次曲线，与图标一致 */}
          <path
            d="M256 214 Q223.3 286.4 170 348 M256 214 Q288.7 286.4 342 348 M170 348 Q256 342.6 342 348"
            fill="none"
            stroke="currentColor"
            strokeOpacity="0.5"
            strokeWidth="22"
            strokeLinecap="round"
          />
        </>
      )}
      {/* 三颗节点：中枢大、成员小 */}
      <circle cx="256" cy="214" r="72" fill="currentColor" />
      <circle cx="170" cy="348" r="48" fill="currentColor" />
      <circle cx="342" cy="348" r="48" fill="currentColor" />
    </svg>
  )
}
