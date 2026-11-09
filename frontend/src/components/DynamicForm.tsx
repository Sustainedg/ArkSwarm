import { useState } from 'react'
import type { ConfigField } from '../lib/types'

interface Props {
  fields: ConfigField[]
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
}

/** 依据智能体的 `config_fields` schema 渲染表单 —— 新增智能体无需改动前端代码。 */
export function DynamicForm({ fields, value, onChange }: Props) {
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
      {advanced.length > 0 && (
        <>
          <button type="button" className="advanced-toggle" onClick={() => setShowAdvanced((v) => !v)}>
            <span>{showAdvanced ? '▾' : '▸'}</span>
            <span>高级设置（{advanced.length}）</span>
          </button>
          {showAdvanced && (
            <div className="form-grid">
              {advanced.map((field) => (
                <Field key={field.key} field={field} value={value[field.key]} onChange={(v) => set(field.key, v)} />
              ))}
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
      const entries = Object.entries((value as Record<string, string>) ?? {})
      const commit = (rows: [string, string][]) =>
        onChange(Object.fromEntries(rows.filter(([k]) => k.trim().length > 0)))
      return (
        <div className="stack" style={{ gap: 8 }}>
          {entries.map(([key, entryValue], index) => (
            <div className="row" key={`${key}-${index}`}>
              <input
                className="input mono"
                value={key}
                placeholder="KEY"
                onChange={(e) => {
                  const rows = entries.map((row) => [...row] as [string, string])
                  rows[index][0] = e.target.value
                  commit(rows)
                }}
              />
              <input
                className="input mono"
                value={entryValue}
                placeholder="value"
                onChange={(e) => {
                  const rows = entries.map((row) => [...row] as [string, string])
                  rows[index][1] = e.target.value
                  commit(rows)
                }}
              />
              <button
                type="button"
                className="btn ghost sm"
                onClick={() => commit(entries.filter((_, i) => i !== index) as [string, string][])}
              >
                ✕
              </button>
            </div>
          ))}
          <button type="button" className="btn sm" onClick={() => commit([...entries, ['', '']] as [string, string][])}>
            + 添加一项
          </button>
        </div>
      )
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
