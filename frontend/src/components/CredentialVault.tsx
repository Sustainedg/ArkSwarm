import { useEffect, useState } from 'react'
import { Modal } from './Modal'
import { api, ApiError } from '../lib/api'
import type { Credential } from '../lib/types'

/**
 * 凭据库（设置页）。
 *
 * 凭据是智能体干活要用的第三方密钥（GitHub token、数据库口令…），与「网关」里的模型密钥
 * 不是一回事：网关密钥属于模型提供方，凭据属于工具 / 服务。这里是它的唯一维护入口 ——
 * 全局一份，智能体在配置里按引用使用；值只经后端加密保存，这里也只显示掩码。
 */
export function CredentialVault() {
  const [items, setItems] = useState<Credential[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Credential | null>(null)

  const reload = async () => {
    setLoading(true)
    try {
      setItems((await api.listCredentials()).items)
      setError(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  const remove = async (credential: Credential) => {
    if (!confirm(`删除凭据「${credential.name}」？`)) return
    try {
      await api.deleteCredential(credential.id)
      await reload()
    } catch (e) {
      setError(describe(e))
    }
  }

  return (
    <section className="stack">
      <div className="row" style={{ gap: 10 }}>
        <h2 className="card-section-title" style={{ margin: 0 }}>
          凭据库
        </h2>
        <div className="spacer" style={{ flex: 1 }} />
        <button className="btn sm" onClick={() => void reload()} disabled={loading}>
          {loading ? <span className="spin">◌</span> : '↻'} 刷新
        </button>
        <button className="btn sm primary" onClick={() => setCreating(true)}>
          + 新建凭据
        </button>
      </div>

      {error && <div className="banner err">{error}</div>}

      <div className="banner info">
        凭据全局一份、可在多个智能体之间共用：值本地加密保存，运行时只以环境变量注入，
        模型只知道变量名（例如 <span className="mono-sm">$GITHUB_TOKEN</span>），
        值不会出现在提示词、日志与接口响应里。智能体配置里的凭据就是从这里挑的。
      </div>

      {items.length === 0 && !loading ? (
        <p className="dim small">还没有凭据。在智能体配置里顺手新建一条，也会出现在这里。</p>
      ) : (
        <div className="grid">
          {items.map((credential) => (
            <article className="card" key={credential.id}>
              <div className="card-head">
                <div style={{ minWidth: 0 }}>
                  <div className="card-title mono-sm">{credential.name}</div>
                  <div className="card-sub">{credential.description ?? '无说明'}</div>
                </div>
                <span className="pill" style={{ marginLeft: 'auto' }}>
                  {credential.masked ?? '未填值'}
                </span>
              </div>
              <div className="chips">
                {credential.used_by_agents > 0 ? (
                  <span className="pill ok">被 {credential.used_by_agents} 个智能体引用</span>
                ) : (
                  <span className="pill">暂无智能体引用</span>
                )}
                <span className="pill">环境变量 ${credential.name}</span>
              </div>
              <div className="card-actions" style={{ marginLeft: 0 }}>
                <button className="btn sm" onClick={() => setEditing(credential)}>
                  编辑
                </button>
                <button className="btn sm danger" onClick={() => void remove(credential)}>
                  删除
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      {(creating || editing) && (
        <CredentialModal
          credential={editing}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={async () => {
            setCreating(false)
            setEditing(null)
            await reload()
          }}
        />
      )}
    </section>
  )
}

/** 新建 / 编辑一条凭据；编辑时值留空＝不换值（界面只回显掩码）。 */
function CredentialModal({
  credential,
  onClose,
  onSaved,
}: {
  credential: Credential | null
  onClose: () => void
  onSaved: () => void
}) {
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
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={credential ? `编辑凭据 · ${credential.name}` : '新建凭据'}
      onClose={onClose}
      footer={
        <>
          <button className="btn ghost" onClick={onClose}>
            取消
          </button>
          <button className="btn primary" onClick={() => void save()} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      {error && <div className="banner err">{error}</div>}
      <div className="field">
        <label className="field-label">名称</label>
        <input
          className="input mono"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="GITHUB_TOKEN"
        />
        <span className="field-help">
          同时就是注入给智能体的环境变量名：以字母或下划线开头，只使用字母、数字与下划线。
        </span>
      </div>
      <div className="field">
        <label className="field-label">值</label>
        <input
          className="input mono"
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
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
          onChange={(e) => setDescription(e.target.value)}
          placeholder="用途说明（会告诉模型这条变量是干什么的）"
        />
      </div>
    </Modal>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
