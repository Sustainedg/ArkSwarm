/**
 * 协作「办公室」：一张等距视角的办公室底图 + 可缩放 / 可拖拽的画布 + 工位上的小人与工牌。
 *
 * 交互（桌面端与移动端共用）：
 * - 滚轮（或双指捏合）以指针所在处为锚点缩放；在空白处按住拖动平移；右下角按钮可放大 / 缩小 / 复位。
 * - 点工牌弹出信息卡：谁、在忙什么、粗略进度；卡上还能跳到该岗位的实时对话。
 * - 工牌上的状态点区分状态：绿＝在线工作、红＝忙碌 / 出问题、黄＝等你决定、灰＝闲置、绿勾＝已完成。
 *
 * 三样东西共用同一套坐标（`lib/collabOffice.ts`，生成脚本直接读它去画底图）：
 * - **人**：每个工位真的坐着一个会动的 3D 小人（`CollabWorker`），落在 `officeChairPoint()`——
 *   底图那把转椅上，跟着底图一起缩放；在干活就一直敲键盘、冒气泡，闲置就趴着睡。
 * - **工牌**：挂在人头顶，写着这是谁（CEO / CTO / 程序员…）与她的职责；缩得太小就只剩头像气泡。
 * - **挂钟**：钟面画在底图里，指针由 `OfficeClock` 按真实时间转——秒针一秒一跳。
 *
 * 拟人化 v2（`docs/architecture.md` §11）：本组件是**唯一持有 tick 与媒体查询的容器**——
 * 一个 5 秒的定时器推进 `tick`，`useMemo` 调 `lib/collabEnv.ts` 的纯函数派生「工位 key → 环境行为」，
 * 再把结果贴成 `.office-worker` 上的 `--seed / --env-x / --env-y / data-seat-key` 与
 * `.collab-station-scene` 上的 `env-* / fidget-*` 类名。派生结果与上一格完全一致就不 `setState`，
 * 免得每 5 秒白渲染一次整张地图。
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from 'react'
import { DUTIES } from '../lib/collabDraft'
import { useThemeMode } from '../lib/theme'
import { ENV_TICK_MS, envScheduleFor, workerSeed, type EnvState } from '../lib/collabEnv'
import {
  OFFICE_HEIGHT, OFFICE_STATUS_TONE, OFFICE_WIDTH, OFFICE_ZOOM, clampOffice, fitOffice, focusOffice,
  officeChairPoint, officeToViewport, panOffice, seatFlowRows, zoomOffice, zoomOfficeAt,
  type OfficeView, type OfficeViewport,
} from '../lib/collabOffice'
import {
  buildFlowRows, agentProgressPercent, type CollabFlowEdge, type CollabAgentProgress,
} from '../lib/collabProgress'
import { CollabWorker } from './CollabWorker'
import { OfficeClock } from './OfficeClock'
import { WorkerAtDesk } from './WorkerAtDesk'

/** 坐着的小人头顶离椅子（落点）多高：画布像素，与 `.office-worker` 的缩放配套。 */
const PERSON_HEAD = 66
/** 工牌吊在人头顶再往上留的空隙：放大地图时工牌始终贴着人，不会飘到半空。 */
const BADGE_GAP = 12
/** 缩到比这个还小就只剩头像气泡——一排名字挤在一起会糊成一片。 */
const LABEL_ZOOM = 0.58
/** 再放大到这个倍数才把职责也写进工牌（工牌变宽，得留够位置）。 */
const DETAIL_ZOOM = 0.64
/** 信息卡的预估尺寸，用来把它收进画布里，别飘到视口外面。 */
const POPOVER_W = 306
const POPOVER_H = 250

interface Props {
  nodes: CollabAgentProgress[]
  edges: CollabFlowEdge[]
  /** 点信息卡上的「查看实时对话」时回调；不传就不显示这个入口。 */
  onOpenNode?: (key: string) => void
  /** 打开时对准哪儿：`whole` 整间办公室（桌面默认），`desks` 只对准工位那一块（窄屏默认）。 */
  initialView?: 'whole' | 'desks'
}

function dutyLabel(duty: string): string {
  return DUTIES.find((item) => item.value === duty)?.label ?? duty
}

/** 两个指针之间的距离（双指捏合缩放用）。 */
function pointerSpread(points: { x: number; y: number }[]): number {
  return Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y)
}

/** 相邻两格的派生结果是否完全一样（一样就不必重渲染整张地图）。 */
function sameSchedule(
  next: Record<string, EnvState>,
  previous: Record<string, EnvState> | null,
): boolean {
  if (!previous) return false
  const keys = Object.keys(next)
  if (keys.length !== Object.keys(previous).length) return false
  return keys.every((key) => {
    const a = next[key]
    const b = previous[key]
    return !!b && a.env === b.env && a.fidget === b.fidget && a.dx === b.dx && a.dy === b.dy
  })
}

export function CollabOfficeMap({ nodes, edges, onOpenNode, initialView = 'whole' }: Props) {
  const { theme } = useThemeMode()
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const pointersRef = useRef(new Map<number, { x: number; y: number }>())
  const pinchRef = useRef<number | null>(null)
  /** 用户是否自己动过视角：动过之后就别再自动「铺满居中」，否则一调整窗口就把视角抢回去。 */
  const interactedRef = useRef(false)

  const [viewport, setViewport] = useState<OfficeViewport>({ width: 0, height: 0 })
  const [view, setView] = useState<OfficeView>({ x: 0, y: 0, zoom: 1 })
  const [focusedKey, setFocusedKey] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)

  const viewportRef = useRef(viewport)
  viewportRef.current = viewport

  const seats = useMemo(() => seatFlowRows(buildFlowRows(nodes, edges)), [nodes, edges])
  const focused = seats.find((seat) => seat.node.key === focusedKey) ?? null

  // —— 拟人化 v2 的调度：tick + 无障碍媒体查询 → 每个工位这一格的环境行为（§11.2 / §11.5）——
  const [tick, setTick] = useState(0)
  const [reducedMotion, setReducedMotion] = useState(false)
  const tickRef = useRef(0)
  const seatsRef = useRef(seats)
  const reducedRef = useRef(reducedMotion)
  seatsRef.current = seats
  reducedRef.current = reducedMotion

  const schedule = useMemo(
    () => envScheduleFor(seats, tick, reducedMotion),
    [seats, tick, reducedMotion],
  )
  /** 当前已经贴到 DOM 上的那一格：下一格派生结果与它相同就不 `setState`。 */
  const appliedRef = useRef<Record<string, EnvState> | null>(null)
  useEffect(() => {
    appliedRef.current = schedule
  }, [schedule])

  useEffect(() => {
    const timer = window.setInterval(() => {
      const next = tickRef.current + 1
      tickRef.current = next
      const candidate = envScheduleFor(seatsRef.current, next, reducedRef.current)
      if (sameSchedule(candidate, appliedRef.current)) return
      setTick(next)
    }, ENV_TICK_MS)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const sync = () => setReducedMotion(query.matches)
    sync()
    query.addEventListener('change', sync)
    return () => query.removeEventListener('change', sync)
  }, [])

  // 画布尺寸变了（打开对话侧栏、转屏…）：没动过就重新铺满居中，动过就只把视角收回合法范围。
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const fit = initialView === 'desks' ? focusOffice : fitOffice
    const apply = (width: number, height: number) => {
      const next = { width, height }
      setViewport(next)
      viewportRef.current = next
      setView((prev) => (interactedRef.current ? clampOffice(prev, next) : fit(next)))
    }
    apply(el.clientWidth, el.clientHeight)
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0].contentRect
      apply(rect.width, rect.height)
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [initialView])

  // 滚轮缩放：React 的 onWheel 是 passive 的，preventDefault 不生效，这里手挂原生监听。
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const rect = el.getBoundingClientRect()
      const focus = { x: event.clientX - rect.left, y: event.clientY - rect.top }
      const factor = event.deltaY < 0 ? OFFICE_ZOOM.step : 1 / OFFICE_ZOOM.step
      interactedRef.current = true
      setView((prev) => zoomOfficeAt(prev, viewportRef.current, factor, focus))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const endPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    pointersRef.current.delete(event.pointerId)
    if (pointersRef.current.size < 2) pinchRef.current = null
    if (pointersRef.current.size === 0) setDragging(false)
  }

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement
    // 点在小人 / 工牌 / 信息卡 / 工具按钮上时不算「拖动画布」，也不收起信息卡。
    if (target.closest('.office-pin, .office-popover, .office-map-tools')) return
    const el = wrapRef.current
    if (!el) return
    el.setPointerCapture(event.pointerId)
    pointersRef.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    if (pointersRef.current.size >= 2) pinchRef.current = pointerSpread([...pointersRef.current.values()])
    setDragging(true)
    setFocusedKey(null)
  }

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const pointers = pointersRef.current
    const previous = pointers.get(event.pointerId)
    if (!previous) return
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY })
    const el = wrapRef.current
    if (!el) return

    if (pointers.size === 1) {
      interactedRef.current = true
      const dx = event.clientX - previous.x
      const dy = event.clientY - previous.y
      setView((prev) => clampOffice(panOffice(prev, dx, dy), viewportRef.current))
      return
    }
    // 双指：按两指距离的比例缩放，锚点是两指中点。
    const points = [...pointers.values()]
    const spread = pointerSpread(points)
    const rect = el.getBoundingClientRect()
    const focus = {
      x: (points[0].x + points[1].x) / 2 - rect.left,
      y: (points[0].y + points[1].y) / 2 - rect.top,
    }
    const before = pinchRef.current
    if (before && before > 0) {
      interactedRef.current = true
      setView((prev) => zoomOfficeAt(prev, viewportRef.current, spread / before, focus))
    }
    pinchRef.current = spread
  }

  const zoomBy = (factor: number) => {
    interactedRef.current = true
    setView((prev) => zoomOffice(prev, viewportRef.current, factor))
  }

  const resetView = () => {
    interactedRef.current = false
    setView(initialView === 'desks' ? focusOffice(viewportRef.current) : fitOffice(viewportRef.current))
  }

  if (nodes.length === 0) return null

  const clamped = (value: number, max: number) => Math.max(8, Math.min(value, Math.max(max, 8)))

  return (
    <div
      className={`office-map${dragging ? ' dragging' : ''}`}
      ref={wrapRef}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endPointer}
      onPointerCancel={endPointer}
      role="group"
      aria-label="协作办公室：可缩放拖拽，点工牌看岗位进度"
    >
      <div
        className="office-map-stage"
        style={{ transform: `translate3d(${view.x}px, ${view.y}px, 0) scale(${view.zoom})` }}
      >
        <img
          className="office-map-bg"
          // 深色 / 浅色各一张底图（同一份几何，见 scripts/gen_office_bg.py）：
          // 浅色外壳下若还用深色图，整块画布就是页面中间的一块黑。
          src={theme === 'light' ? '/office/iso-office-light.svg' : '/office/iso-office.svg'}
          alt="等距视角的办公室"
          width={OFFICE_WIDTH}
          height={OFFICE_HEIGHT}
          draggable={false}
        />
        {/* 每个工位上真的坐着一个会动的 3D 小人：跟着底图一起缩放，所以永远踩在自己那把转椅上 */}
        <div className="office-map-people" aria-hidden="true">
          {seats.map((seat) => {
            const chair = officeChairPoint(seat.desk)
            const seed = workerSeed(seat.node.key)
            const env = schedule[seat.node.key]
            return (
              <span
                key={seat.node.key}
                className="office-worker"
                data-seat-key={seat.node.key}
                style={{
                  left: chair.x,
                  top: chair.y,
                  '--seed': seed,
                  // 走位以**画布像素**挂在父层上（自定义属性会继承），身体层再按 --office-scale 换算。
                  '--env-x': `${env?.dx ?? 0}px`,
                  '--env-y': `${env?.dy ?? 0}px`,
                } as CSSProperties}
              >
                <span className={`collab-station-scene state-${seat.node.state}${seat.node.isLive ? ' live' : ''}${env?.env ? ` env-${env.env}` : ''}${env?.fidget ? ` fidget-${env.fidget}` : ''}`}>
                  <CollabWorker state={seat.node.state} env={env?.env ?? null} seed={seed} />
                </span>
              </span>
            )
          })}
        </div>
        <OfficeClock />
      </div>

      <div className="office-map-layer">
        {seats.map((seat) => {
          const tone = OFFICE_STATUS_TONE[seat.node.state]
          const point = officeToViewport(view, officeChairPoint(seat.desk))
          const labelled = view.zoom >= LABEL_ZOOM
          const detailed = view.zoom >= DETAIL_ZOOM
          const duty = seat.node.isEntry ? '领导者' : dutyLabel(seat.node.duty)
          return (
            <button
              key={seat.node.key}
              type="button"
              className={`office-pin tone-${tone.tone}${labelled ? ' labelled' : ''}${detailed ? ' detailed' : ''}${seat.node.isLive ? ' live' : ''}${seat.node.key === focusedKey ? ' selected' : ''}`}
              style={{ left: point.x, top: point.y - (PERSON_HEAD * view.zoom + BADGE_GAP) }}
              onClick={() => setFocusedKey(seat.node.key)}
              title={`${seat.node.label} · ${duty} · ${tone.label}`}
              aria-label={`${seat.node.label}（${duty}），${tone.label}，点开看进度`}
            >
              <span className="office-pin-body">
                <span className="office-pin-face" aria-hidden="true">{seat.node.avatar}</span>
                <span className="office-pin-text">
                  <span className="office-pin-name">{seat.node.label}</span>
                  <span className="office-pin-duty">{duty}</span>
                </span>
                <span className="office-pin-dot" aria-hidden="true" />
              </span>
            </button>
          )
        })}
      </div>

      {focused && (() => {
        const tone = OFFICE_STATUS_TONE[focused.node.state]
        const percent = agentProgressPercent(focused.node)
        const anchor = officeToViewport(view, officeChairPoint(focused.desk))
        const wantRight = anchor.x + 26 + POPOVER_W <= viewport.width
        const badgeTop = anchor.y - (PERSON_HEAD * view.zoom + BADGE_GAP)
        return (
          <div
            className="office-popover"
            style={{
              left: clamped(wantRight ? anchor.x + 26 : anchor.x - 26 - POPOVER_W, viewport.width - POPOVER_W),
              top: clamped(badgeTop - 56, viewport.height - POPOVER_H),
            }}
            role="dialog"
            aria-label={`${focused.node.label} 的进度`}
          >
            <div className="office-popover-head">
              <span className="office-popover-scene" aria-hidden="true">
                <WorkerAtDesk state={focused.node.state} live={focused.node.isLive} />
              </span>
              <span className="office-popover-id">
                <span className="office-popover-name">{focused.node.avatar} {focused.node.label}</span>
                <span className="office-popover-duty">
                  {dutyLabel(focused.node.duty)}{focused.node.isEntry ? ' · 领导者' : ''}
                </span>
                <span className={`office-popover-status tone-${tone.tone}`}>
                  <i aria-hidden="true" />{focused.node.stateLabel}
                </span>
              </span>
              <button
                type="button"
                className="office-popover-close"
                onClick={() => setFocusedKey(null)}
                aria-label="关闭"
              >
                ✕
              </button>
            </div>

            <div className="office-popover-progress">
              <div className="office-popover-progress-head">
                <span>粗略进度</span>
                <b>{percent}%</b>
              </div>
              <div className="office-progress-track">
                <span className={`office-progress-fill tone-${tone.tone}`} style={{ width: `${percent}%` }} />
              </div>
            </div>

            <p className="office-popover-summary">{focused.node.summary}</p>
            <div className="office-popover-meta">
              <span>{focused.node.stepCount > 0 ? `跑了 ${focused.node.stepCount} 步` : '还没参与这一轮'}</span>
              {focused.node.lastAction && <span>最近动作：{focused.node.lastAction}</span>}
              {focused.node.activeTickets.length > 0 && (
                <span>{focused.node.activeTickets.length} 张工单在处理</span>
              )}
            </div>

            {onOpenNode && (
              <button
                type="button"
                className="btn ghost sm office-popover-more"
                onClick={() => {
                  onOpenNode(focused.node.key)
                  setFocusedKey(null)
                }}
              >
                查看实时对话 →
              </button>
            )}
          </div>
        )
      })()}

      <div className="office-map-tools">
        <button type="button" onClick={() => zoomBy(OFFICE_ZOOM.step)} aria-label="放大">＋</button>
        <button type="button" onClick={() => zoomBy(1 / OFFICE_ZOOM.step)} aria-label="缩小">－</button>
        <button type="button" onClick={resetView} aria-label="复位视角">⤢</button>
      </div>

      <div className="office-map-legend" aria-hidden="true">
        <span className="office-tone-working"><i />工作中</span>
        <span className="office-tone-busy"><i />忙碌</span>
        <span className="office-tone-ask"><i />等你决定</span>
        <span className="office-tone-idle"><i />闲置</span>
        <span className="office-tone-done"><i />已完成</span>
      </div>

      <div className="office-map-hint" aria-hidden="true">滚轮 / 双指缩放 · 拖动平移 · 点工牌看进度</div>
    </div>
  )
}
