import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

/**
 * 网络代理设置（设置页）。
 *
 * 配置一个代理地址（HTTP 或 SOCKS），注入到安装 / 更新 / 查询版本等子进程的环境变量里，
 * 让内核的安装命令能通过代理访问 GitHub / npm registry 等外部站点。
 * 桌面端与移动端共用此组件。
 */
export function ProxySettings() {
  const [url, setUrl] = useState('')
  const [saved, setSaved] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const reload = async () => {
    setLoading(true)
    try {
      const result = await api.getProxy()
      setUrl(result.proxy_url ?? '')
      setSaved(result.proxy_url ?? '')
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

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const result = await api.setProxy(url.trim() || null)
      setSaved(result.proxy_url ?? '')
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  const clear = async () => {
    setSaving(true)
    setError(null)
    try {
      await api.setProxy(null)
      setUrl('')
      setSaved(null)
    } catch (e) {
      setError(describe(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="stack">
      <h2 className="card-section-title">网络代理</h2>
      <div className="card">
        <p className="dim small" style={{ marginBottom: 8 }}>
          为安装 / 更新 / 查询版本等命令配置代理，支持 HTTP 和 SOCKS 代理。留空则不使用代理。
        </p>
        {error && <div className="banner err">{error}</div>}
        <div className="row wrap" style={{ gap: 8, alignItems: 'flex-end' }}>
          <div style={{ flex: 1, minWidth: 200 }}>
            <label className="field-label">代理地址</label>
            <input
              className="input"
              type="text"
              value={url}
              placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:1080"
              onChange={(e) => setUrl(e.target.value)}
              disabled={loading || saving}
            />
          </div>
          <button className="btn primary" onClick={() => void save()} disabled={loading || saving}>
            {saving ? '保存中…' : '保存'}
          </button>
          {saved && (
            <button className="btn" onClick={() => void clear()} disabled={loading || saving}>
              清除
            </button>
          )}
        </div>
        {saved && (
          <p className="dim small" style={{ marginTop: 8 }}>
            当前代理：{saved}
          </p>
        )}
      </div>
    </section>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
