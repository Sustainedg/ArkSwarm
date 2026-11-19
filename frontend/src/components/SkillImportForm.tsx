import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { MarketKernel, SkillTransferResult } from '../lib/types'
import { readFileAsBase64, validateSkillArchive } from '../lib/skillFiles'

/**
 * 导入技能表单（两端共用）：选择目标内核 + 选择一个 zip 压缩包。
 *
 * 弹层外壳交给两端各自的组件（桌面端 `Modal`、移动端 `MobileSheet`），
 * 这里只负责内核列表、文件校验与提交。
 */
export function SkillImportForm({
  onImported,
  onCancel,
}: {
  onImported: (result: SkillTransferResult, kernelName: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [kernels, setKernels] = useState<MarketKernel[]>([])
  const [kernel, setKernel] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .marketKernels()
      .then((items) => {
        setKernels(items)
        setKernel((current) => current || items[0]?.type_id || '')
      })
      .catch((e) => setError(describe(e)))
  }, [])

  const submit = async () => {
    if (!kernel) {
      setError('请先选择一个目标内核')
      return
    }
    if (!file) {
      setError('请先选择一个技能压缩包')
      return
    }
    const invalid = validateSkillArchive(file)
    if (invalid) {
      setError(invalid)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const content_base64 = await readFileAsBase64(file)
      const result = await api.importSkill({ type_id: kernel, name: file.name, content_base64 })
      await onImported(result, kernels.find((item) => item.type_id === kernel)?.name ?? kernel)
    } catch (e) {
      setError(describe(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack" style={{ gap: 14 }}>
      <label className="field">
        <span className="field-label">选择内核</span>
        <select
          className="select"
          value={kernel}
          disabled={busy || kernels.length === 0}
          onChange={(event) => setKernel(event.target.value)}
        >
          {kernels.length === 0 && <option value="">（没有可写入技能目录的内核）</option>}
          {kernels.map((item) => (
            <option key={item.type_id} value={item.type_id}>
              {item.name}
            </option>
          ))}
        </select>
        <span className="field-help">
          技能会解压到该内核自己的用户技能目录；导入后仍需在智能体里分配才会生效。
        </span>
      </label>

      <label className="field">
        <span className="field-label">技能压缩包</span>
        <input
          className="input"
          type="file"
          accept=".zip,application/zip"
          disabled={busy}
          onChange={(event) => {
            setFile(event.target.files?.[0] ?? null)
            setError(null)
          }}
        />
        <span className="field-help">
          支持 .zip，包内需要有 SKILL.md（推荐放一层同名目录）。单个技能不超过 20 MB。
        </span>
      </label>

      {error && <div className="banner err">{error}</div>}

      <div className="row" style={{ gap: 9, justifyContent: 'flex-end' }}>
        <button className="btn ghost" onClick={onCancel} disabled={busy}>
          取消
        </button>
        <button className="btn primary" onClick={() => void submit()} disabled={busy}>
          {busy ? '导入中…' : '导入技能'}
        </button>
      </div>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
