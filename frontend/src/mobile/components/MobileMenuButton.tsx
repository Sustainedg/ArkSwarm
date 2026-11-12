import { createContext, useContext, type ReactNode } from 'react'

/** 打开左侧抽屉的回调：由 `MobileApp` 注入，页面只负责触发。 */
const MobileMenuContext = createContext<() => void>(() => undefined)

/** 外壳里注入打开抽屉的能力，页面里的菜单按钮/空状态按钮才能调用。 */
export function MobileMenuProvider({ onOpen, children }: { onOpen: () => void; children: ReactNode }) {
  return <MobileMenuContext.Provider value={onOpen}>{children}</MobileMenuContext.Provider>
}

/** 页面里拿「打开左侧抽屉」这个动作。 */
export function useMobileMenu(): () => void {
  return useContext(MobileMenuContext)
}

/**
 * 顶栏左上角的菜单按钮（应用 logo）：点它打开左侧抽屉。
 *
 * 移动端只有这一处主导航，所有顶级页面的顶栏都默认用这个按钮（见 `MobileTopBar`）。
 */
export function MobileMenuButton() {
  const open = useMobileMenu()
  return (
    <button className="m-logo-btn" onClick={open} aria-label="打开导航菜单">
      <span className="m-brand-mark">✦</span>
    </button>
  )
}
