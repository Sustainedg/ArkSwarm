import { useCallback, useState } from 'react'

/**
 * 折叠分组的展开状态：按 key 单独记，未被用户点过的分组用 `defaultOpen`。
 *
 * 技能页（按内核）默认折叠；智能体页（按分组）默认展开 —— 一进页面就能看到卡片，
 * 想收起来再点标题。两端页面共用这一份，别各写一套 Set 的增删。
 *
 * ```tsx
 * const { isExpanded, toggle } = useExpandMap()
 * <CollapsibleGroup expanded={isExpanded(key)} onToggle={() => toggle(key)} … />
 * ```
 */
export function useExpandMap(defaultOpen = false) {
  const [overrides, setOverrides] = useState<Record<string, boolean>>({})
  const toggle = useCallback(
    (key: string) => {
      setOverrides((prev) => ({ ...prev, [key]: !(prev[key] ?? defaultOpen) }))
    },
    [defaultOpen],
  )
  return { isExpanded: (key: string) => overrides[key] ?? defaultOpen, toggle }
}
