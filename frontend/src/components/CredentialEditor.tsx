import { useMemo, useState } from 'react'
import type { AgentCredentialBrief, AgentCredentialInput, Credential } from '../lib/types'

/**
 * 界面上的草稿行。
 *
 * `id` 有值＝引用了凭据库里已有的那条（值只读，改值去「设置 → 凭据库」）；
 * 没有 `id` 但 `name` 命中库里的名字时，保存时也会解析成引用那条；
 * 都不命中就是「新凭据」—— 保存时连同 `value` 一起写进凭据库。
 */
export interface CredentialDraft {
  id?: string
  name: string
  value: string
}

/** 已引用的凭据 → 草稿行（值一律清空，界面只回显掩码）。 */
export function credentialsToDraft(items: AgentCredentialBrief[] | undefined): CredentialDraft[] {
  return (items ?? []).map((item) => ({ id: item.id, name: item.name, value: '' }))
}

/** 草稿行 → 接口载荷；引用了库里已有凭据时不带 `value`（不改它的值）。 */
export function draftsToPayload(drafts: CredentialDraft[]): AgentCredentialInput[] {
  return drafts.map((draft) => {
    const value = draft.value || null
    return value === null
      ? { id: draft.id ?? null, name: draft.name.trim() }
      : { id: draft.id ?? null, name: draft.name.trim(), value }
  })
}

/** 校验草稿行；有问题返回一句可直接展示的中文，全部合法返回 null。 */
export function validateCredentials(drafts: CredentialDraft[], library: Credential[]): string | null {
  const seen = new Set<string>()
  for (const [index, draft] of drafts.entries()) {
    const name = draft.name.trim()
    if (!name) return `第 ${index + 1} 条凭据还没选名字`
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(name)) {
      return `凭据名「${name}」不能用作环境变量名：请以字母或下划线开头，只使用字母、数字与下划线`
    }
    if (seen.has(name)) return `凭据名「${name}」重复了，请改成不同的名字`
    seen.add(name)
    const known = draft.id || library.some((item) => item.name === name)
    if (!known && !draft.value.trim()) return `凭据「${name}」是新的，请填上它的值`
  }
  return null
}

/**
 * 凭据编辑器（桌面与移动端共用）。
 *
 * 点名字输入框会弹出**凭据库**里已有的数据：直接挑一条就引用它（值留在库里，只以环境变量注入）；
 * 也可以继续输入一个新名字 + 值，保存智能体时那条凭据会自动进入凭据库。
 */
export function CredentialEditor({
  drafts,
  library,
  onChange,
}: {
  drafts: CredentialDraft[]
  library: Credential[]
  onChange: (next: CredentialDraft[]) => void
}) {
  const [openIndex, setOpenIndex] = useState<number | null>(null)

  const patch = (index: number, part: Partial<CredentialDraft>) =>
    onChange(drafts.map((item, i) => (i === index ? { ...item, ...part } : item)))

  const picked = useMemo(
    () => new Set(drafts.map((item) => item.name.trim()).filter(Boolean)),
    [drafts],
  )

  return (
    <div className="stack" style={{ gap: 8 }}>
      {drafts.length === 0 && (
        <span className="field-help">
          {library.length > 0
            ? '点下面的「＋ 添加凭据」，名字输入框会列出凭据库里已有的数据。'
            : '还没有凭据。填一个名字与值，保存后它会进入凭据库，之后别的智能体也能直接挑。'}
        </span>
      )}
      {drafts.map((draft, index) => {
        const known = draft.id
          ? library.find((item) => item.id === draft.id)
          : library.find((item) => item.name === draft.name.trim())
        const options = library.filter((item) => !picked.has(item.name) || item.name === draft.name.trim())
        return (
          <div className="credential-row" key={index}>
            <div className="credential-name">
              <input
                className="input mono"
                value={draft.name}
                onChange={(e) => {
                  // 改了名字就当作「新凭据」，除非重新挑一条
                  patch(index, { name: e.target.value, id: undefined })
                  setOpenIndex(index)
                }}
                onFocus={() => setOpenIndex(index)}
                onBlur={() => window.setTimeout(() => setOpenIndex(null), 120)}
                placeholder="点这里挑一条，或输入新名字"
                aria-label="凭据名"
              />
              {openIndex === index && (
                <div className="credential-menu">
                  {options.length === 0 ? (
                    <div className="credential-menu-empty">
                      凭据库是空的，直接输入名字即可新建一条。
                    </div>
                  ) : (
                    options.map((item) => (
                      <button
                        type="button"
                        className="credential-menu-item"
                        key={item.id}
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          patch(index, { id: item.id, name: item.name, value: '' })
                          setOpenIndex(null)
                        }}
                      >
                        <span className="mono-sm">{item.name}</span>
                        <span className="dim small">
                          {item.masked ? `已保存 ${item.masked}` : '未填值'}
                          {item.used_by_agents > 0 ? ` · ${item.used_by_agents} 个智能体在用` : ''}
                        </span>
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
            {known ? (
              <span className="credential-value-note">
                引用凭据库里的 {known.name}（{known.masked ?? '未填值'}）· 值在「设置 → 凭据库」里维护
              </span>
            ) : (
              <input
                className="input"
                type="password"
                value={draft.value}
                onChange={(e) => patch(index, { value: e.target.value })}
                placeholder="凭据值（保存后进入凭据库）"
                aria-label="凭据值"
              />
            )}
            <button
              type="button"
              className="btn ghost sm"
              onClick={() => onChange(drafts.filter((_, i) => i !== index))}
              aria-label={`移除凭据「${draft.name || index + 1}」`}
            >
              ✕
            </button>
          </div>
        )
      })}
      <div>
        <button
          type="button"
          className="btn sm"
          onClick={() => {
            onChange([...drafts, { name: '', value: '' }])
            setOpenIndex(drafts.length)
          }}
        >
          + 添加凭据
        </button>
      </div>
    </div>
  )
}
