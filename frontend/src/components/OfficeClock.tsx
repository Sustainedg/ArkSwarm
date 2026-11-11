/**
 * 墙上的挂钟：底图只画**钟面**（表盘 + 刻度，`public/office/iso-office.svg` 的 `.office-clock-face`），
 * 指针在这里按**真实时间**画——时针 / 分针 / 秒针的角度都取自系统时钟，秒针一秒一跳，
 * 和真的挂在墙上的钟一样。圆心与半径来自 `lib/collabOffice.ts` 的 `OFFICE_CLOCK`（与底图同一份坐标）。
 */
import { useEffect, useState } from 'react'
import { OFFICE_CLOCK } from '../lib/collabOffice'

/** 三个指针的角度（12 点方向为 0°、顺时针为正）。 */
function handAngles(now: Date): { hour: number; minute: number; second: number } {
  const second = now.getSeconds() + now.getMilliseconds() / 1000
  const minute = now.getMinutes() + second / 60
  const hour = (now.getHours() % 12) + minute / 60
  return { hour: hour * 30, minute: minute * 6, second: second * 6 }
}

export function OfficeClock() {
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    // 每跳一次都对到「整秒」边界上，走久了也不会和系统时间差出一截。
    let timer = 0
    const tick = () => {
      setNow(new Date())
      timer = window.setTimeout(tick, 1000 - (Date.now() % 1000))
    }
    timer = window.setTimeout(tick, 1000 - (Date.now() % 1000))
    return () => window.clearTimeout(timer)
  }, [])

  const angle = handAngles(now)
  return (
    <span className="office-clock" style={{ left: OFFICE_CLOCK.x, top: OFFICE_CLOCK.y }} aria-hidden="true">
      <span className="office-clock-hand office-clock-hour" style={{ transform: `rotate(${angle.hour}deg)` }} />
      <span className="office-clock-hand office-clock-minute" style={{ transform: `rotate(${angle.minute}deg)` }} />
      <span className="office-clock-hand office-clock-second" style={{ transform: `rotate(${angle.second}deg)` }} />
      <span className="office-clock-cap" />
    </span>
  )
}
