/**
 * 协作「办公室」视图的纯逻辑：工位坐标、热区落座规则，以及画布的平移 / 缩放数学。
 *
 * 画布坐标系与背景图 `frontend/public/office/iso-office.svg` 的 viewBox 完全一致
 * （左上角为原点、单位是像素）。背景图由 `scripts/gen_office_bg.py` 生成，脚本会读本文件的
 * `OFFICE_DESKS` 去画桌子 —— 所以热区永远落在桌子上；两边不一致时守卫测试
 * `backend/tests/test_frontend_assets.py` 会拦下来。
 *
 * 工位按「一行一层」摆：第 0 行只坐入口领导者一人，第 1 行坐它直接派活的那个人（通常是 CTO），
 * 再往下每行并排坐干活的人 —— 与协作流程图的分层一一对应，谁在哪个位置一眼看明白。
 *
 * 这里不碰 DOM、不发请求，桌面端与移动端共用。
 */
import type { CollabAgentState } from './collabProgress'

/** 办公室画布的尺寸（也是背景图的 viewBox）。 */
export const OFFICE_WIDTH = 1600
export const OFFICE_HEIGHT = 1000

/** 一张桌子的桌面中心（画布坐标）。 */
export interface OfficeDesk {
  x: number
  y: number
}

/** 每一行摆几张桌子：领导者一人一行、CTO 一人一行，往下是干活的一排。 */
export const OFFICE_ROW_SIZES: readonly number[] = [1, 1, 4, 4]

/**
 * 工位（桌子）坐标，**按行、由里到外**排列：先第 0 行（领导者，房间最里面，一人一桌），
 * 再第 1 行，再是下面两排干活的。行与行的分界见 `OFFICE_ROW_SIZES`。
 */
export const OFFICE_DESKS: readonly OfficeDesk[] = [
  { x: 776, y: 328 },
  { x: 776, y: 424 },
  { x: 608, y: 519 },
  { x: 720, y: 519 },
  { x: 832, y: 519 },
  { x: 944, y: 519 },
  { x: 630, y: 614 },
  { x: 742, y: 614 },
  { x: 854, y: 614 },
  { x: 966, y: 614 },
]

/** 按 `OFFICE_ROW_SIZES` 把工位切成一行行 —— 一行的工位坐同一层的岗位。 */
export const OFFICE_DESK_ROWS: readonly (readonly OfficeDesk[])[] = (() => {
  const rows: OfficeDesk[][] = []
  let cursor = 0
  for (const size of OFFICE_ROW_SIZES) {
    if (cursor >= OFFICE_DESKS.length) break
    rows.push(OFFICE_DESKS.slice(cursor, cursor + size))
    cursor += size
  }
  if (cursor < OFFICE_DESKS.length) rows.push(OFFICE_DESKS.slice(cursor))
  return rows
})()

/**
 * 转椅相对**桌心**的偏移（画布坐标）：与底图 `desk_group()` 画的椅子一致（`iso(ix, iy + 0.74)`）。
 * 人是坐在椅子上、面朝显示器，所以小人的落点用 `officeChairPoint()`，而不是桌心。
 */
export const OFFICE_CHAIR_OFFSET = { x: -41.4, y: 20.7 } as const

/** 一个人坐在哪：桌心挪到转椅的位置（可能带上 `LAP_OFFSET` 的错开量）。 */
export function officeChairPoint(desk: OfficeDesk): OfficeDesk {
  return { x: desk.x + OFFICE_CHAIR_OFFSET.x, y: desk.y + OFFICE_CHAIR_OFFSET.y }
}

/**
 * 墙上的挂钟（圆心 + 半径，画布坐标）。
 *
 * 底图只画**钟面**（表盘 + 刻度，`public/office/iso-office.svg` 里的 `.office-clock-face`），
 * 指针由前端 `components/OfficeClock.tsx` 按**真实时间**画 —— 秒针一秒一跳，和墙上的真钟一样。
 * 生成脚本直接读这里的三个数，所以钟面与指针永远对得上（`backend/tests/test_frontend_assets.py` 守着）。
 */
export const OFFICE_CLOCK = { x: 204.8, y: 399.6, r: 21 } as const

/** 一个岗位坐在哪张桌子上；`lap` 是「第几圈」（一层的人比这一排的桌子多时绕回来的圈数）。 */
export interface OfficeSeat<T> {
  node: T
  desk: OfficeDesk
  lap: number
}

/** 绕回来的人每圈往后、往右各错开一点，避免两个人（连带工牌）叠在一起。 */
const LAP_OFFSET = { x: 18, y: 34 }

/**
 * 把「一行一层的岗位」安置到工位上：第 N 层的岗位坐第 N 排桌子。
 *
 * 一层的人比这一排桌子多时（例如某层塞了 5 个人），多出来的人从这排第一张桌子重新坐起，
 * 但整圈往后错开，人和工牌都不会叠在一起；层数比桌子排数还多时，多余的层落进最后一排继续错开。
 */
export function seatFlowRows<T extends { key: string }>(rows: { nodes: T[] }[]): OfficeSeat<T>[] {
  if (OFFICE_DESK_ROWS.length === 0) return []
  const seats: OfficeSeat<T>[] = []
  rows.forEach((row, rowIndex) => {
    const bandIndex = Math.min(rowIndex, OFFICE_DESK_ROWS.length - 1)
    const band = OFFICE_DESK_ROWS[bandIndex]
    const spill = rowIndex - bandIndex
    row.nodes.forEach((node, index) => {
      const desk = band[index % band.length]
      const lap = Math.floor(index / band.length) + spill
      seats.push({
        node,
        lap,
        desk: { x: desk.x + lap * LAP_OFFSET.x, y: desk.y + lap * LAP_OFFSET.y },
      })
    })
  })
  return seats
}

/** 工牌上状态点的「状态色」：绿点在线工作、红点忙到出事、黄点等你拍板、灰点闲置、绿勾收工。 */
export interface OfficeStatusTone {
  /** 状态点与进度条共用的类名后缀（`.office-tone-*`）。 */
  tone: 'working' | 'busy' | 'ask' | 'idle' | 'done'
  /** 悬停 / 信息卡上写给用户看的一句话。 */
  label: string
}

export const OFFICE_STATUS_TONE: Record<CollabAgentState, OfficeStatusTone> = {
  active: { tone: 'working', label: '在线工作中' },
  error: { tone: 'busy', label: '忙碌 / 出问题了' },
  waiting: { tone: 'ask', label: '在等你决定' },
  pending: { tone: 'idle', label: '闲置待命' },
  done: { tone: 'done', label: '已完成' },
}

/** 画布的「看哪儿」：平移量（视口坐标）与缩放倍数。 */
export interface OfficeView {
  x: number
  y: number
  zoom: number
}

export interface OfficeViewport {
  width: number
  height: number
}

/** 缩放范围：0.5 倍能看全整间办公室，2.4 倍够看清屏幕上的代码。 */
export const OFFICE_ZOOM = { min: 0.5, max: 2.4, step: 1.22 } as const

/**
 * 办公区（工位所在的那块画布）：手机屏幕小，默认先对准这里，想看全屋再往外缩。
 *
 * 框的是「人 + 工牌 + 桌子」那一块（最里面那排人的工牌也要框进来），所以窄屏打开时
 * 正好是工位特写：小人、工牌上的名字都看得清，而不是缩成一小团。
 */
export const OFFICE_FOCUS = { x: 505, y: 172, width: 540, height: 496 } as const

/** 「整间房铺满视口」的缩放倍数（contain）。 */
function fitZoom(viewport: OfficeViewport): number {
  if (viewport.width <= 0 || viewport.height <= 0) return 1
  return Math.min(viewport.width / OFFICE_WIDTH, viewport.height / OFFICE_HEIGHT)
}

/**
 * 缩放下限：桌面端 0.5 倍就够看全整间办公室；手机屏幕窄，得能缩得比 0.5 更小，
 * 否则「铺满视口」会被 0.5 顶住，一打开就只能看到办公室的一个角。
 */
export function minZoom(viewport?: OfficeViewport): number {
  if (!viewport) return OFFICE_ZOOM.min
  return Math.min(OFFICE_ZOOM.min, fitZoom(viewport) * 0.9)
}

/** 缩放到 [下限, max] 之间；给了视口就按视口算下限（窄屏能缩得更小）。 */
export function clampZoom(zoom: number, viewport?: OfficeViewport): number {
  return Math.min(OFFICE_ZOOM.max, Math.max(minZoom(viewport), zoom))
}

/** 把整间办公室铺满视口（contain）并居中——打开时的默认视角。 */
export function fitOffice(viewport: OfficeViewport): OfficeView {
  const { width, height } = viewport
  if (width <= 0 || height <= 0) return { x: 0, y: 0, zoom: clampZoom(1, viewport) }
  const zoom = clampZoom(fitZoom(viewport), viewport)
  return centerOffice({ x: 0, y: 0, zoom }, viewport)
}

/** 对准办公区（工位那一块）并居中——窄屏上的默认视角，比「看全整间房」看得清。 */
export function focusOffice(viewport: OfficeViewport, rect: { x: number; y: number; width: number; height: number } = OFFICE_FOCUS): OfficeView {
  const { width, height } = viewport
  if (width <= 0 || height <= 0) return { x: 0, y: 0, zoom: clampZoom(1, viewport) }
  const zoom = clampZoom(Math.min(width / rect.width, height / rect.height), viewport)
  return clampOffice({
    zoom,
    x: width / 2 - (rect.x + rect.width / 2) * zoom,
    y: height / 2 - (rect.y + rect.height / 2) * zoom,
  }, viewport)
}

/** 保持缩放不变，把画布摆到视口正中。 */
export function centerOffice(view: OfficeView, viewport: OfficeViewport): OfficeView {
  return {
    ...view,
    x: (viewport.width - OFFICE_WIDTH * view.zoom) / 2,
    y: (viewport.height - OFFICE_HEIGHT * view.zoom) / 2,
  }
}

/** 平移：把画布往某个方向推（正数 = 往右 / 往下）。 */
export function panOffice(view: OfficeView, dx: number, dy: number): OfficeView {
  return { ...view, x: view.x + dx, y: view.y + dy }
}

/**
 * 以视口里的某个点（例如鼠标所在处）为锚点缩放：缩放前后，该点下面的画布内容不动。
 */
export function zoomOfficeAt(
  view: OfficeView,
  viewport: OfficeViewport,
  factor: number,
  focus: { x: number; y: number },
): OfficeView {
  const zoom = clampZoom(view.zoom * factor, viewport)
  if (zoom === view.zoom) return clampOffice(view, viewport)
  const worldX = (focus.x - view.x) / view.zoom
  const worldY = (focus.y - view.y) / view.zoom
  return clampOffice(
    { zoom, x: focus.x - worldX * zoom, y: focus.y - worldY * zoom },
    viewport,
  )
}

/** 以视口中心缩放（给「＋ / −」按钮用）。 */
export function zoomOffice(view: OfficeView, viewport: OfficeViewport, factor: number): OfficeView {
  return zoomOfficeAt(view, viewport, factor, { x: viewport.width / 2, y: viewport.height / 2 })
}

/**
 * 收住平移量：画布比视口大时不许拖出空白（留一点边距），比视口小时居中。
 */
export function clampOffice(view: OfficeView, viewport: OfficeViewport): OfficeView {
  const { width, height } = viewport
  const zoom = clampZoom(view.zoom, viewport)
  const canvasWidth = OFFICE_WIDTH * zoom
  const canvasHeight = OFFICE_HEIGHT * zoom
  const slack = 64
  const axis = (offset: number, viewportSize: number, canvasSize: number) => {
    if (canvasSize + slack * 2 <= viewportSize) return (viewportSize - canvasSize) / 2
    const min = viewportSize - canvasSize - slack
    return Math.min(slack, Math.max(min, offset))
  }
  return {
    zoom,
    x: axis(view.x, width, canvasWidth),
    y: axis(view.y, height, canvasHeight),
  }
}

/** 画布坐标 → 视口坐标（信息卡与工牌要贴在人旁边时用）。 */
export function officeToViewport(view: OfficeView, point: { x: number; y: number }): { x: number; y: number } {
  return { x: view.x + point.x * view.zoom, y: view.y + point.y * view.zoom }
}
