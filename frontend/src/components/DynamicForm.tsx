import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import type { ConfigField } from '../lib/types'

interface Props {
  fields: ConfigField[]
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
  /** 追加在「高级设置」里的额外字段（例如凭据编辑器），与适配器的高级项同一个折叠区。 */
  advancedExtra?: ReactNode
}

/** 依据智能体的 `config_fields` schema 渲染表单 —— 新增智能体无需改动前端代码。 */
export function DynamicForm({ fields, value, onChange, advancedExtra }: Props) {
  const [showAdvanced, setShowAdvanced] = useState(false)
  const basics = fields.filter((f) => !f.advanced)
  const advanced = fields.filter((f) => f.advanced)

  const set = (key: string, next: unknown) => onChange({ ...value, [key]: next })

  return (
    <div className="stack">
      <div className="form-grid">
        {basics.map((field) => (
          <Field key={field.key} field={field} value={value[field.key]} onChange={(v) => set(field.key, v)} />
        ))}
      </div>
      {(advanced.length > 0 || advancedExtra) && (
        <>
          <button type="button" className="advanced-toggle" onClick={() => setShowAdvanced((v) => !v)}>
            <span>{showAdvanced ? '▾' : '▸'}</span>
            <span>高级设置（{advanced.length + (advancedExtra ? 1 : 0)}）</span>
          </button>
          {showAdvanced && (
            <div className="form-grid">
              {advanced.map((field) => (
                <Field key={field.key} field={field} value={value[field.key]} onChange={(v) => set(field.key, v)} />
              ))}
              {advancedExtra}
            </div>
          )}
        </>
      )}
    </div>
  )
}

function Field({
  field,
  value,
  onChange,
}: {
  field: ConfigField
  value: unknown
  onChange: (next: unknown) => void
}) {
  const span = field.type === 'textarea' || field.type === 'keyvalue' ? ' span-2' : ''
  return (
    <div className={`field${span}`}>
      <label className="field-label">
        {field.label}
        {field.required && <span className="pill err">必填</span>}
      </label>
      {renderControl(field, value, onChange)}
      {field.help && <span className="field-help">{field.help}</span>}
    </div>
  )
}

function renderControl(field: ConfigField, value: unknown, onChange: (v: unknown) => void) {
  const placeholder = field.placeholder ?? undefined

  switch (field.type) {
    case 'boolean':
      return (
        <label className="switch">
          <input
            type="checkbox"
            checked={Boolean(value ?? field.default)}
            onChange={(e) => onChange(e.target.checked)}
          />
          <span>{value ? '已启用' : '已关闭'}</span>
        </label>
      )

    case 'number':
      return (
        <input
          className="input mono"
          type="number"
          placeholder={placeholder}
          value={value === undefined || value === null ? '' : String(value)}
          onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
        />
      )

    case 'select':
      return (
        <select className="select" value={String(value ?? '')} onChange={(e) => onChange(e.target.value)}>
          {!field.options?.length && <option value="">—</option>}
          {field.options?.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )

    case 'secret':
      return (
        <input
          className="input mono"
          type="password"
          autoComplete="new-password"
          placeholder={placeholder ?? '••••••••'}
          value={String(value ?? '')}
          onChange={(e) => onChange(e.target.value)}
        />
      )

    case 'textarea':
      return (
        <textarea
          className="textarea"
          placeholder={placeholder}
          value={String(value ?? '')}
          onChange={(e) => onChange(e.target.value)}
        />
      )

    case 'string_list': {
      const list = Array.isArray(value) ? (value as string[]) : []
      return (
        <textarea
          className="textarea"
          style={{ minHeight: 64 }}
          placeholder={placeholder ?? '每行一个值'}
          value={list.join('\n')}
          onChange={(e) =>
            onChange(
              e.target.value
                .split('\n')
                .map((line) => line.trim())
                .filter(Boolean),
            )
          }
        />
      )
    }

    case 'keyvalue': {
      return <KeyValueField value={value} onChange={onChange} />
    }

    default:
      return (
        <input
          className="input"
          placeholder={placeholder}
          value={String(value ?? '')}
          onChange={(e) => onChange(e.target.value)}
        />
      )
  }
}

/**
 * 键值对编辑器（`FieldType.KEYVALUE`）。
 *
 * 草稿行放在组件内部：空行只是「还没填完」，不能立刻被过滤掉，否则「＋ 添加一项」看起来毫无反应。
 * 只有键非空的行才会写回表单值。
 */
function KeyValueField({ value, onChange }: { value: unknown; onChange: (next: unknown) => void }) {
  const stored = useMemo(() => (value as Record<string, string>) ?? {}, [value])
  const [rows, setRows] = useState<[string, string][]>(() => Object.entries(stored))
  // 记住上次自己吐出去的值：外部把它换掉（例如切换内核重置 options）时才重建草稿行。
  const emitted = useRef<Record<string, string>>(stored)

  useEffect(() => {
    if (emitted.current === stored) return
    emitted.current = stored
    setRows(Object.entries(stored))
  }, [stored])

  const commit = (next: [string, string][]) => {
    setRows(next)
    const packed = Object.fromEntries(next.filter(([key]) => key.trim().length > 0))
    emitted.current = packed
    onChange(packed)
  }

  const patch = (index: number, column: 0 | 1, text: string) => {
    const next = rows.map((row) => [...row] as [string, string])
    next[index][column] = text
    commit(next)
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      {rows.map(([key, entryValue], index) => (
        <div className="row" key={index}>
          <input
            className="input mono"
            value={key}
            placeholder="键"
            onChange={(e) => patch(index, 0, e.target.value)}
          />
          <input
            className="input mono"
            value={entryValue}
            placeholder="值"
            onChange={(e) => patch(index, 1, e.target.value)}
          />
          <button
            type="button"
            className="btn ghost sm"
            onClick={() => commit(rows.filter((_, i) => i !== index))}
            aria-label="删除这一项"
          >
            ✕
          </button>
        </div>
      ))}
      <button type="button" className="btn sm" onClick={() => commit([...rows, ['', '']])}>
        + 添加一项
      </button>
    </div>
  )
}
