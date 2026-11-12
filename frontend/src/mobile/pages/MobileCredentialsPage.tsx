import { useCallback, useEffect, useState } from 'react'
import { api } from '../../lib/api'
import type { Credential } from '../../lib/types'
import { MobileSheet } from '../components/MobileSheet'
import { MobileTopBar } from '../components/MobileTopBar'
import { describeError } from '../util'

/**
 * 凭据库（移动端）。
 *
 * 凭据是智能体干活要用的第三方密钥（GitHub token、数据库口令…），与「网关」里的模型密钥
 * 不是一回事：网关密钥属于模型提供方，凭据属于工具 / 服务。这里只显示名字与掩码，
 * 智能体在配置里按引用使用。
 */
export function MobileCredentialsPage() {
  const [items, setItems] = useState<Credential[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Credential | null>(null)

  const reload = useCallback(async () => {
    try {
      setItems((await api.listCredentials()).items)
      setError(null)
    } catch (e) {
      setError(describeError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const remove = async (credential: Credential) => {
    if (!confirm(`删除凭据「${credential.name}」？`)) return
    try {
      await api.deleteCredential(credential.id)
      await reload()
    } catch (e) {
      setError(describeError(e))
    }
  }

  return (
    <div className="m-page">
      <MobileTopBar
        title="凭据库"
        subtitle="工具与服务要用的密钥"
        actions={
          <button className="m-icon-btn" onClick={() => setCreating(true)} aria-label="新建凭据">
            ＋
          </button>
        }
      />

      <div className="m-page-body">
        <div className="stack">
          {error && <div className="banner err">{error}</div>}
          <div className="banner info">
            凭据全局一份、可在多个智能体之间共用：值本地加密保存，运行时只以环境变量注入，
            模型只知道变量名，值不会出现在提示词、日志与接口响应里。
          </div>

          {loading ? (
            <p className="dim small">正在加载凭据…</p>
          ) : items.length === 0 ? (
            <div className="m-empty">
              <div className="m-empty-icon">🔑</div>
              <p>还没有凭据</p>
              <button className="btn primary" onClick={() => setCreating(true)}>
                ＋ 新建凭据
              </button>
            </div>
          ) : (
            <div className="m-list">
              {items.map((credential) => (
                <div className="card" key={credential.id}>
                  <div className="row">
                    <div className="m-item-main">
                      <div className="m-item-title mono-sm">{credential.name}</div>
                      <div className="m-item-sub">{credential.description ?? '无说明'}</div>
                      <div className="dim small">环境变量 ${credential.name}</div>
                    </div>
                    <span className="pill">{credential.masked ?? '未填值'}</span>
                  </div>
                  <div className="row wrap" style={{ gap: 6 }}>
                    {credential.used_by_agents > 0 ? (
                      <span className="pill ok">{credential.used_by_agents} 个智能体在用</span>
                    ) : (
                      <span className="pill">暂无智能体引用</span>
                    )}
                  </div>
                  <div className="m-item-actions" style={{ marginTop: 0 }}>
                    <button className="btn sm" onClick={() => setEditing(credential)}>
                      编辑
                    </button>
                    <button className="btn sm danger" onClick={() => void remove(credential)}>
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {(creating || editing) && (
        <MobileSheet
          title={editing ? `编辑凭据 · ${editing.name}` : '新建凭据'}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
        >
          <CredentialForm
            credential={editing}
            onSaved={async () => {
              setCreating(false)
              setEditing(null)
              await reload()
            }}
          />
        </MobileSheet>
      )}
    </div>
  )
}

/** 新建 / 编辑一条凭据；编辑时值留空＝不换值（界面只回显掩码）。 */
function CredentialForm({ credential, onSaved }: { credential: Credential | null; onSaved: () => void }) {
  const [name, setName] = useState(credential?.name ?? '')
  const [description, setDescription] = useState(credential?.description ?? '')
  const [value, setValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    if (!name.trim()) return setError('请填写凭据名称')
    if (!credential && !value) return setError('请填写凭据值')
    setSaving(true)
    setError(null)
    try {
      if (credential) {
        await api.updateCredential(credential.id, {
          name: name.trim(),
          description: description.trim() || null,
          value: value || null,
        })
      } else {
        await api.createCredential({
          name: name.trim(),
          description: description.trim() || null,
          value,
        })
      }
      onSaved()
    } catch (e) {
      setError(describeError(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="stack">
      {error && <div className="banner err">{error}</div>}
      <div className="field">
        <label className="field-label">名称</label>
        <input
          className="input mono"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="GITHUB_TOKEN"
        />
        <span className="field-help">同时就是注入给智能体的环境变量名。</span>
      </div>
      <div className="field">
        <label className="field-label">值</label>
        <input
          className="input mono"
          type="password"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={credential ? `已保存：${credential.masked ?? '—'}（留空则不改）` : 'sk-…'}
          autoComplete="new-password"
        />
        <span className="field-help">本地加密保存，保存后接口只回掩码，明文永不再下发。</span>
      </div>
      <div className="field">
        <label className="field-label">说明</label>
        <input
          className="input"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="用途说明（可选）"
        />
      </div>
      <button className="btn primary block" onClick={() => void save()} disabled={saving}>
        {saving ? '保存中…' : '保存'}
      </button>
    </div>
  )
}
