import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { MarketKernel, SkillDetail, SkillTransferResult } from '../lib/types'

/**
 * 「复制技能到别的内核」表单（两端共用）：选择目标内核并提交。
 *
 * 只复制技能文件，不复制智能体分配；目标内核里已有同名技能时由后端拒绝。
 */
export function SkillCopyForm({
  skill,
  onCopied,
  onCancel,
}: {
  skill: SkillDetail
  onCopied: (result: SkillTransferResult, kernelName: string) => void | Promise<void>
  onCancel: () => void
}) {
  const [kernels, setKernels] = useState<MarketKernel[]>([])
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .marketKernels()
      .then((items) => {
        const candidates = items.filter((item) => item.type_id !== skill.type_id)
        setKernels(candidates)
        setTarget((current) => current || candidates[0]?.type_id || '')
      })
      .catch((e) => setError(describe(e)))
  }, [skill.type_id])

  const submit = async () => {
    if (!target) {
      setError('请先选择一个目标内核')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await api.copySkill(skill.id, target)
      await onCopied(result, kernels.find((item) => item.type_id === target)?.name ?? target)
    } catch (e) {
      setError(describe(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack" style={{ gap: 14 }}>
      <p className="dim small">
        把「{skill.name}」的文件复制到另一个内核的技能目录。只复制技能本体，不复制智能体分配；
        目标内核里已有同名技能时不会覆盖。
      </p>
      <label className="field">
        <span className="field-label">目标内核</span>
        <select
          className="select"
          value={target}
          disabled={busy || kernels.length === 0}
          onChange={(event) => setTarget(event.target.value)}
        >
          {kernels.length === 0 && <option value="">（没有其它可写入技能的内核）</option>}
          {kernels.map((item) => (
            <option key={item.type_id} value={item.type_id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>

      {error && <div className="banner err">{error}</div>}

      <div className="row" style={{ gap: 9, justifyContent: 'flex-end' }}>
        <button className="btn ghost" onClick={onCancel} disabled={busy}>
          取消
        </button>
        <button
          className="btn primary"
          onClick={() => void submit()}
          disabled={busy || kernels.length === 0}
        >
          {busy ? '复制中…' : '开始复制'}
        </button>
      </div>
    </div>
  )
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status} · ${error.message}`
  return error instanceof Error ? error.message : String(error)
}
