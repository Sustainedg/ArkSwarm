/**
 * 智能体头像选择器（编辑表单第一位）：大头像预览 + 随机生成可爱动图 + 自定义上传 + emoji 快选。
 *
 * - **随机生成**：从可爱动物 / 表情 emoji 池里随机挑一个，配上随机的小动作（摇摆 / 浮动 /
 *   蹦跳 —— 见 `.agent-avatar-live` 的三套动画）。生成的头像「会动」，比一排静态按钮有生气。
 * - **自定义上传**：base64 走 `PUT /api/agents/{id}/avatar`（与用户头像同一条管线）。
 *   新建流程是「先建后传」：表单先保存拿到智能体 id，页面再补一次上传。
 * - **emoji 快选**：原有的固定 emoji 池保留，作为兜底。
 *
 * 受控组件：`emoji`（当前 emoji 头像）由表单持有；`avatarFileId` 是已上传头像的
 * 文件名标识（上传成功后由页面写回，保存时不用再传 —— 后端在上传接口里已落库）。
 */
import { useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { readFileAsBase64, validateAvatarFile } from '../lib/avatar'
import { RefreshIcon } from './Icons'

/** 可爱的动物 / 小生物 emoji 池：随机生成从这里挑。 */
const CUTE_AVATARS = [
  '🐱', '🐶', '🐰', '🦊', '🐻', '🐼', '🐨', '🐯', '🦁', '🐮',
  '🐷', '🐸', '🐵', '🐔', '🐧', '🦉', '🦄', '🐙', '🦋', '🐳',
  '🍈', '🍄', '🌻', '⭐', '🌙', '🌈', '🍭', '🎈', '🎨', '🪄',
]

/** 随机动作池：三选一（CSS 动画类，见 index.css 的 .agent-avatar-live-*）。 */
const LIVE_ANIMATIONS = ['swing', 'float', 'bounce'] as const

/** 随机生成一枚「可爱的会动的头像」：emoji + 动作。 */
function randomLiveAvatar(): { emoji: string; animation: (typeof LIVE_ANIMATIONS)[number] } {
  return {
    emoji: CUTE_AVATARS[Math.floor(Math.random() * CUTE_AVATARS.length)],
    animation: LIVE_ANIMATIONS[Math.floor(Math.random() * LIVE_ANIMATIONS.length)],
  }
}

export function AgentAvatarPicker({
  agentId,
  emoji,
  onEmojiChange,
  avatarFileId,
  onUploaded,
}: {
  /** 已保存的智能体 id；新建流程还没保存时为 null（此时「上传」置灰，提示先保存） */
  agentId: string | null
  emoji: string
  onEmojiChange: (emoji: string) => void
  /** 当前自定义头像的文件名标识（有值 = 已上传，预览显示图而不是 emoji） */
  avatarFileId?: string | null
  /** 上传 / 删除成功后回传更新后的智能体 */
  onUploaded: (agent: { id: string; avatar_file?: string | null }) => void
}) {
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 随机生成时的小动作（选完 emoji 后它一直动着，页面有生气） */
  const [liveAnimation, setLiveAnimation] = useState<(typeof LIVE_ANIMATIONS)[number] | null>(null)

  const generate = () => {
    const next = randomLiveAvatar()
    onEmojiChange(next.emoji)
    setLiveAnimation(next.animation)
    // 换了 emoji 头像，自定义头像图就不再显示（后端以 avatar_file 优先，这里同步删掉）。
    if (agentId && avatarFileId) void removeFile()
  }

  const upload = async (file: File) => {
    if (!agentId) {
      setError('先保存智能体，再上传自定义头像')
      return
    }
    const invalid = validateAvatarFile(file)
    if (invalid) {
      setError(invalid)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const base64 = await readFileAsBase64(file)
      const next = await api.uploadAgentAvatar(agentId, { content_base64: base64, name: file.name })
      onUploaded(next)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '头像上传失败')
    } finally {
      setBusy(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  const removeFile = async () => {
    if (!agentId) return
    setBusy(true)
    setError(null)
    try {
      const next = await api.deleteAgentAvatar(agentId)
      onUploaded(next)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '移除失败')
    } finally {
      setBusy(false)
    }
  }

  const hasFile = Boolean(agentId && avatarFileId)

  return (
    <div className="field">
      <label className="field-label">头像</label>
      <div className="agent-avatar-picker">
        {/* 大头像预览：已上传图 > emoji（随机生成的会一直做小动作） */}
        <div className={`agent-avatar-preview${liveAnimation && !hasFile ? ` live-${liveAnimation}` : ''}`}>
          {hasFile ? (
            <img
              src={`/api/agents/${agentId}/avatar?v=${avatarFileId}`}
              alt="智能体头像"
              onError={(event) => {
                // 文件丢了（换了数据目录之类）：退回 emoji，不显示裂图
                (event.target as HTMLImageElement).style.display = 'none'
              }}
            />
          ) : (
            <span className="agent-avatar-emoji">{emoji}</span>
          )}
        </div>
        <div className="agent-avatar-actions">
          <button type="button" className="btn sm" onClick={generate} title="随机生成一枚可爱的会动的头像">
            <RefreshIcon size={13} /> 随机生成
          </button>
          <button
            type="button"
            className="btn sm"
            disabled={busy || !agentId}
            onClick={() => inputRef.current?.click()}
            title={agentId ? '上传自定义头像（PNG / JPEG / WebP，≤2 MB）' : '先保存智能体，再上传头像'}
          >
            上传头像
          </button>
          {hasFile && (
            <button type="button" className="btn sm ghost" disabled={busy} onClick={() => void removeFile()}>
              移除图片
            </button>
          )}
          <input
            ref={inputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void upload(file)
              event.target.value = ''
            }}
          />
        </div>
      </div>
      {error && <span className="field-help" style={{ color: 'var(--danger)' }}>{error}</span>}
      {/* emoji 快选：兜底的固定池 */}
      <div className="row wrap" style={{ gap: 6, marginTop: 8 }}>
        {CUTE_AVATARS.slice(0, 18).map((item) => (
          <button
            key={item}
            type="button"
            className={`agent-avatar-emoji-btn${item === emoji && !hasFile ? ' picked' : ''}`}
            onClick={() => {
              onEmojiChange(item)
              setLiveAnimation(null)
              if (agentId && avatarFileId) void removeFile()
            }}
            aria-label={`头像 ${item}`}
          >
            {item}
          </button>
        ))}
      </div>
    </div>
  )
}
