import { AccountPanel } from '../../components/AccountPanel'
import { UserAdminPanel } from '../../components/UserAdminPanel'
import { useAuth } from '../../components/AuthGate'
import { MobileTopBar } from '../components/MobileTopBar'

/**
 * 移动端账号页：个人资料 / 修改口令；管理员再多一块「用户管理」。
 *
 * 与桌面端「设置 → 账号 / 用户」共用同一份组件（`AccountPanel` / `UserAdminPanel`），
 * 这里只负责移动端的外壳（顶栏 + 页面内边距）。
 */
export function MobileAccountPage() {
  const { user, isAdmin } = useAuth()
  return (
    <div className="m-page">
      <MobileTopBar title="账号" subtitle={user?.display_name || user?.username || undefined} />
      <div className="m-page-body">
        <AccountPanel />
        {isAdmin && <UserAdminPanel />}
      </div>
    </div>
  )
}
