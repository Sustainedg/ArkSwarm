/**
 * 头像上传区（两端共用）：大头像 + 「更换 / 移除」两个动作。
 *
 * 交互按「选图即上传」做：用户点「更换头像」→ 系统文件选择器 → 本地校验（格式 / 大小）
 * → 立即上传 → 保存资料接口回传新用户。不做「先选后点保存」，因为头像和显示名 / 邮箱
 * 是两条独立的保存路径（后端也是独立接口），混在一起只会让人以为「不点保存就不生效」。
 *
 * 单用户模式（鉴权关闭）下不渲染。
 */
import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { readFileAsBase64, validateAvatarFile } from '../lib/avatar'
import { saveUser } from '../lib/auth'
import type { AuthUser } from '../lib/types'
import { useAuth } from './AuthGate'
import { UserAvatar } from './UserAvatar'

export function AvatarUploader({ user }: { user: AuthUser }) {
  const { refreshUser } = useAuth()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  // 本地刚选中的图先顶上去：上传通常几百毫秒，等接口回来再换会闪一下。
  const [preview, setPreview] = useState<string | null>(null)
  // preview 是个 object URL，必须显式回收：换了下一张、或组件卸载时才 revoke，
  // 上传成功的那一刻不能 revoke —— 那会让正在显示的预览变成裂图。
  const previewRef = useRef<string | null>(null)

  const setPreviewUrl = (next: string | null) => {
    if (previewRef.current && previewRef.current !== next) URL.revokeObjectURL(previewRef.current)
    previewRef.current = next
    setPreview(next)
  }

  useEffect(
    () => () => {
      if (previewRef.current) URL.revokeObjectURL(previewRef.current)
    },
    [],
  )

  const pick = () => inputRef.current?.click()

  const apply = (next: AuthUser) => {
    saveUser(next)
    refreshUser(next)
  }

  const upload = async (file: File) => {
    const invalid = validateAvatarFile(file)
    if (invalid) {
      setError(invalid)
      setMessage(null)
      return
    }
    setBusy(true)
    setError(null)
    setMessage(null)
    setPreviewUrl(URL.createObjectURL(file))
    try {
      const base64 = await readFileAsBase64(file)
      const next = await api.uploadAvatar({ content_base64: base64, name: file.name })
      apply(next)
      setMessage('头像已更新')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '头像上传失败')
      setPreviewUrl(null)
    } finally {
      setBusy(false)
      // 允许重复选同一个文件（否则 change 事件不会再触发）
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  const remove = async () => {
    setBusy(true)
    setError(null)
    setMessage(null)
    try {
      const next = await api.deleteAvatar()
      apply(next)
      setPreviewUrl(null)
      setMessage('已移除头像')
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '移除失败')
    } finally {
      setBusy(false)
    }
  }

  const name = user.display_name || user.username

  return (
    <div className="avatar-editor">
      <div className="avatar-editor-pic">
        {preview ? (
          <span
            className="user-avatar"
            style={{ ['--avatar-size' as string]: '72px' } as React.CSSProperties}
          >
            <img src={preview} alt="" />
          </span>
        ) : (
          <UserAvatar user={user} size={72} />
        )}
        <div className="avatar-editor-meta">
          <div className="avatar-editor-name">{name}</div>
          <div className="dim small">PNG / JPEG / WebP，不超过 2 MB</div>
        </div>
      </div>

      <div className="row wrap" style={{ gap: 8 }}>
        <button type="button" className="btn sm" onClick={pick} disabled={busy}>
          {busy ? '处理中…' : user.avatar ? '更换头像' : '上传头像'}
        </button>
        {user.avatar && (
          <button type="button" className="btn sm" onClick={() => void remove()} disabled={busy}>
            移除
          </button>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0]
          if (file) void upload(file)
        }}
      />
      {error && <div className="banner err">{error}</div>}
      {message && <div className="banner ok">{message}</div>}
    </div>
  )
}
