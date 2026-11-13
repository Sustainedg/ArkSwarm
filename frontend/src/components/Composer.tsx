/**
 * 输入区（**两端共享**）：对话页与协作「找 CEO 聊」都用这一份，样式与交互完全一致。
 *
 * 框里从左到右只有四样东西：附件（可选）、语音、输入框、发送（运行中变成停止）。
 * 输入框**没有 placeholder**：框里一长串提示文案在手机上既挤又容易跟真实输入混淆，
 * 光秃秃的输入框配图标反而更清楚。
 *
 * 附件与语音都收在这里，页面只提供数据与回调：
 * - `value` / `onChange`：草稿文字（页面持有，才能按场景清空与预填）；
 * - `attachments` / `onAttachmentsChange`：待发附件（页面持有，发送成功后由页面清空）；
 * - `onUpload`：把读好的 base64 交给页面调接口（不传就不显示回形针）；
 * - `onSend`：正文与附件都空时不触发（除非页面开了 `allowEmptySend`），页面从自己的状态里取附件。
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MutableRefObject,
  type ReactNode,
  type Ref,
} from 'react'
import type { AttachmentRef } from '../lib/types'
import { describeSpeechError, finalTranscript, speechRecognitionCtor, type SpeechRecognizer } from '../lib/speech'
import { MicIcon, PaperclipIcon, SendIcon, StopIcon } from './Icons'

/** 单个附件的上限，与服务端 `MAX_ATTACHMENT_BYTES` 保持一致（前端先拦一道，省一次往返）。 */
const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

/** 输入框长高的上限（px）。与样式表里 `.composer textarea` 的 `max-height` 是同一个数。 */
const MAX_INPUT_HEIGHT = 160

interface Props {
  /** 草稿文字 */
  value: string
  onChange: (value: string) => void
  /** 发送（正文与附件都空时不会触发） */
  onSend: () => void
  /** 待发送的附件 */
  attachments: AttachmentRef[]
  onAttachmentsChange: (next: AttachmentRef[]) => void
  /** 上传一个附件（回形针）；不传就不显示回形针 */
  onUpload?: (name: string, contentBase64: string) => Promise<AttachmentRef>
  /** 运行中：右侧按钮变成停止 */
  onStop?: () => void
  busy?: boolean
  /** 整块禁用（例如还没选对话、另一台设备正在跑） */
  disabled?: boolean
  /** 回车直接发送（默认回车换行；桌面端用键盘时开这个） */
  submitOnEnter?: boolean
  /** 空内容也能发送：协作的「推进任务」模式下，不补充内容就是让领导者继续推进 */
  allowEmptySend?: boolean
  /** 附件与语音的提示 / 报错回给页面显示 */
  onNotice?: (text: string) => void
  /** 输入框的无障碍名称 */
  inputLabel?: string
  /** 输入框引用：页面需要主动聚焦时传入（例如点「自由答复」跳到输入框） */
  inputRef?: Ref<HTMLTextAreaElement>
  /** 框上方的一条说明（token 用量、协作状态…）：贴着输入框，但不进框里 */
  header?: ReactNode
  /** 框下方的一条说明（用量统计之类）：贴着输入框下沿，也不进框里 */
  footer?: ReactNode
  /** 额外的类名：页面按自己的版式微调输入区（例如移动端对话页把文字放到图标上面） */
  className?: string
  /**
   * 运行中也允许继续输入 / 发送：发送交给页面排队，本轮回复结束后由页面自动发出。
   * 不开时沿用旧行为（运行中输入框置灰，右侧只有停止）。
   */
  queueWhenBusy?: boolean
}

/** 读成 base64（去掉 data URL 前缀），交给上传接口。 */
function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result ?? '')
      const comma = result.indexOf(',')
      resolve(comma >= 0 ? result.slice(comma + 1) : result)
    }
    reader.onerror = () => reject(new Error(`读取附件「${file.name}」失败`))
    reader.readAsDataURL(file)
  })
}

export function Composer({
  value,
  onChange,
  onSend,
  attachments,
  onAttachmentsChange,
  onUpload,
  onStop,
  busy = false,
  disabled = false,
  submitOnEnter = false,
  allowEmptySend = false,
  onNotice,
  inputLabel = '输入消息',
  inputRef,
  header,
  footer,
  className,
  queueWhenBusy = false,
}: Props) {
  const [uploading, setUploading] = useState(false)
  const [listening, setListening] = useState(false)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const areaRef = useRef<HTMLTextAreaElement | null>(null)
  const voiceRef = useRef<SpeechRecognizer | null>(null)
  /**
   * 语音识别是否可用。**按钮一直显示**：独立应用（加到主屏）里 iOS 常常没有
   * `webkitSpeechRecognition`，藏起来用户只会以为功能没了；点一下给中文说明更好懂。
   */
  const voiceSupported = useMemo(() => Boolean(speechRecognitionCtor()), [])
  const locked = disabled || (busy && !queueWhenBusy)

  // 卸载（离开页面 / 关掉弹层）时收起麦克风，别让识别在后台空转。
  useEffect(() => () => voiceRef.current?.abort(), [])

  /**
   * 输入框随内容长高，最多 `MAX_INPUT_HEIGHT`。
   *
   * **必须盯着 `value` 量，而不是只在键盘 `onChange` 里量**：草稿由页面持有，发送 / 换对话 /
   * 预填都是页面把它清掉或改掉，不会经过输入框的 `onChange` —— 只在 `onChange` 里量的话，
   * 写满一屏再发出去，框还是原来那么高，收不回来。
   *
   * 用 `useLayoutEffect` 在绘制前量：先归零再读 `scrollHeight`（`height: auto` 那一步），
   * 否则读到的永远是当前高度、只会越量越高。
   */
  useLayoutEffect(() => {
    const node = areaRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(node.scrollHeight, MAX_INPUT_HEIGHT)}px`
  }, [value])

  /**
   * 输入框同时挂两个 ref：页面传进来的（要主动聚焦 / 取节点）与本组件内部量高度用的。
   * 一个元素只接受一个 `ref`，所以在这里合并转发。
   */
  const setAreaRef = useCallback(
    (node: HTMLTextAreaElement | null) => {
      areaRef.current = node
      if (!inputRef) return
      if (typeof inputRef === 'function') inputRef(node)
      else (inputRef as MutableRefObject<HTMLTextAreaElement | null>).current = node
    },
    [inputRef],
  )

  const notify = useCallback(
    (text: string) => {
      onNotice?.(text)
    },
    [onNotice],
  )

  const pick = async (files: FileList | null) => {
    if (!files || files.length === 0 || !onUpload) return
    if (locked) {
      notify('当前还不能添加附件')
      return
    }
    setUploading(true)
    try {
      const uploaded: AttachmentRef[] = []
      for (const file of Array.from(files)) {
        if (file.size > MAX_ATTACHMENT_BYTES) {
          notify(`「${file.name}」超过 ${MAX_ATTACHMENT_BYTES / 1024 / 1024} MB，换个小一点的文件`)
          continue
        }
        const encoded = await readBase64(file)
        uploaded.push(await onUpload(file.name, encoded))
      }
      if (uploaded.length > 0) onAttachmentsChange([...attachments, ...uploaded])
    } catch (e) {
      notify((e as Error)?.message || '附件上传失败')
    } finally {
      setUploading(false)
    }
  }

  const toggleVoice = () => {
    if (listening) {
      voiceRef.current?.stop()
      setListening(false)
      return
    }
    const Recognizer = speechRecognitionCtor()
    if (!Recognizer) {
      notify('当前环境不支持语音输入（独立应用到主屏时浏览器常常禁用），可以用键盘上的听写')
      return
    }
    const recognizer = new Recognizer()
    recognizer.lang = 'zh-CN'
    recognizer.continuous = true
    recognizer.interimResults = false
    recognizer.onresult = (event) => {
      const text = finalTranscript(event)
      if (text) onChange(value ? `${value}${text}` : text)
    }
    recognizer.onerror = (event) => {
      notify(describeSpeechError(event.error))
      setListening(false)
    }
    recognizer.onend = () => setListening(false)
    voiceRef.current = recognizer
    try {
      recognizer.start()
      setListening(true)
    } catch {
      notify('语音输入启动失败，请重试')
      setListening(false)
    }
  }

  const canSend = allowEmptySend || Boolean(value.trim() || attachments.length > 0)
  const submit = () => {
    if (locked || !canSend) return
    onSend()
  }

  return (
    <div className={className ? `composer ${className}` : 'composer'}>
      {header && <div className="composer-head">{header}</div>}

      {attachments.length > 0 && (
        <div className="composer-attach-row">
          {attachments.map((item) => (
            <span className="composer-attach-chip" key={item.rel_path}>
              <span className="composer-attach-name">{item.name}</span>
              <button
                onClick={() =>
                  onAttachmentsChange(attachments.filter((row) => row.rel_path !== item.rel_path))
                }
                aria-label={`移除附件 ${item.name}`}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="composer-box">
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(event) => {
            void pick(event.target.files)
            // 清空 value：同一个文件连选两次也要能触发 change
            event.target.value = ''
          }}
        />
        {onUpload && (
          <button
            className="composer-tool"
            onClick={() => fileRef.current?.click()}
            disabled={locked || uploading}
            aria-label="添加附件"
            title="添加附件（图片 / 文档都行，会上传到工作目录）"
          >
            {uploading ? <span className="composer-tool-spin">◌</span> : <PaperclipIcon size={17} />}
          </button>
        )}
        <button
          className={`composer-tool${listening ? ' on' : ''}${voiceSupported ? '' : ' muted'}`}
          onClick={toggleVoice}
          disabled={locked}
          aria-label={listening ? '结束语音输入' : '语音输入'}
          title={
            voiceSupported
              ? listening
                ? '正在听…点一下结束'
                : '语音输入（按浏览器识别转成文字）'
              : '当前环境不支持语音输入，点一下看说明'
          }
        >
          <MicIcon size={17} />
        </button>
        <textarea
          ref={setAreaRef}
          rows={1}
          value={value}
          aria-label={inputLabel}
          disabled={locked}
          // 高度不在这里量：草稿可能由页面清空（发送 / 换对话），只有盯着 `value` 的 effect 才收得回来。
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (!submitOnEnter) return
            if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
            event.preventDefault()
            submit()
          }}
        />
        {/* 运行中只留一个按钮：框里写了东西就是「排队发送」，空着就是「停止」。
            这样手指永远落在同一个位置，也不会出现「想停止却点到发送」。 */}
        {busy && (!queueWhenBusy || !canSend) && (
          <button className="composer-tool stop" onClick={() => onStop?.()} aria-label="停止" title="停止这次运行">
            <StopIcon size={15} />
          </button>
        )}
        {(!busy || (queueWhenBusy && canSend)) && (
          <button
            className="composer-tool send"
            onClick={submit}
            disabled={disabled || !canSend}
            aria-label={busy ? '排队发送' : '发送'}
            title={busy ? '排队发送：本轮回复结束后自动发出' : '发送'}
          >
            <SendIcon size={18} />
          </button>
        )}
      </div>

      {footer && <div className="composer-foot">{footer}</div>}
    </div>
  )
}
