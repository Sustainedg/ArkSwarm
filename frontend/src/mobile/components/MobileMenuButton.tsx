import { createContext, useContext, type ReactNode } from 'react'
import { MenuIcon } from '../../components/Icons'

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
 * 顶栏左上角的折叠按钮（汉堡三横线）：点它打开左侧抽屉。
 *
 * 移动端只有这一处主导航，所有顶级页面的顶栏都默认用这个按钮（见 `MobileTopBar`）。
 * 原来这里是应用 logo —— 但顶栏左上角放 logo 占了「导航入口」的心智位，
 * 用户想找菜单时第一反应是找汉堡图标，就换成它了（品牌感留在抽屉与登录页）。
 */
export function MobileMenuButton() {
  const open = useMobileMenu()
  return (
    <button className="m-icon-btn" onClick={open} aria-label="打开导航菜单">
      <MenuIcon size={20} />
    </button>
  )
}
