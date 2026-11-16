/**
 * 朗读（语音合成）：把智能体的输出念出来。
 *
 * 只有一份实现 —— 开关存在 localStorage 的 `arkswarm.tts` 上，**默认关闭**（用户没主动开过就不出声）。
 * 使用时先经 `toSpeechText()` 把 Markdown 还原成能听的纯文本：`**加粗**`、代码围栏、
 * 表格分隔线这些念出来只有噪音。一次只念一条：新输出进来会打断上一条。
 *
 * 与 `lib/theme.ts` 同属「这台设备的个人偏好」：不上传、不落库，换台设备各管各的。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

/** 存储键（值只写 `'on'`；没这个键就是关闭）。 */
export const SPEECH_STORAGE_KEY = 'arkswarm.tts'

/** 浏览器是否支持语音合成（iOS Safari / Chrome 都支持，个别裁剪过的内核对不上）。 */
export function speechSupported(): boolean {
  return typeof window !== 'undefined' && 'speechSynthesis' in window
}

/** 读开关；没设置过就是关闭（默认不出声）。 */
export function readSpeechEnabled(): boolean {
  try {
    return localStorage.getItem(SPEECH_STORAGE_KEY) === 'on'
  } catch {
    /* 隐私模式下 localStorage 可能不可用：按默认关闭处理 */
    return false
  }
}

/** 写开关；关闭时把键删掉，保持「默认关闭」只有一个真值来源。 */
export function storeSpeechEnabled(enabled: boolean): void {
  try {
    if (enabled) localStorage.setItem(SPEECH_STORAGE_KEY, 'on')
    else localStorage.removeItem(SPEECH_STORAGE_KEY)
  } catch {
    /* 写不进去就只在本次会话里生效 */
  }
}

/**
 * 把 Markdown 还原成「能听」的纯文本：
 * 代码块整段丢掉（念出来是噪音），行内代码与链接只留内容，标题 / 引用 / 列表符号去掉，
 * 表格与分隔线降级成停顿，段落与换行变成句读 —— 语音合成读标点才有自然的停顿。
 */
export function toSpeechText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, '。') // 代码块：整段跳过
    .replace(/`([^`]*)`/g, '$1') // 行内代码：只留内容
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1') // 图片：只读替代文字
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // 链接：只读标题
    .replace(/^\s{0,3}#{1,6}\s*/gm, '') // 标题
    .replace(/^\s{0,3}>\s?/gm, '') // 引用
    .replace(/^\s{0,3}(?:[-*+]|\d+[.)])\s+/gm, '') // 列表项
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1$2')
    .replace(/~~([^~]+)~~/g, '$1')
    .replace(/^\s*[-*_]{3,}\s*$/gm, '。') // 分隔线
    .replace(/^\s*\|?[\s|:-]+\|?\s*$/gm, '') // 表格的「|---|」分隔行：没内容可念
    .replace(/^\s*\|.*\|\s*$/gm, (row) => row.replace(/\|/g, ' ')) // 表格行
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{2,}/g, '。')
    .replace(/\n/g, '，')
    .replace(/[。，]{2,}/g, '。') // 相邻的句读并成一个，免得念出一串停顿
    .trim()
}

/** 单段文字的长度上限：太长的文本部分浏览器会静默截断，按句子切开排队念。 */
const CHUNK_LIMIT = 180

/** 按句读把长文切成小段（切点优先标点，其次硬切）。 */
function chunkText(text: string): string[] {
  const chunks: string[] = []
  let rest = text
  while (rest.length > CHUNK_LIMIT) {
    const window = rest.slice(0, CHUNK_LIMIT)
    const cut = Math.max(
      window.lastIndexOf('。'),
      window.lastIndexOf('！'),
      window.lastIndexOf('？'),
      window.lastIndexOf('；'),
      window.lastIndexOf('，'),
    )
    const end = cut > CHUNK_LIMIT / 3 ? cut + 1 : CHUNK_LIMIT
    chunks.push(rest.slice(0, end).trim())
    rest = rest.slice(end)
  }
  if (rest.trim()) chunks.push(rest.trim())
  return chunks.filter(Boolean)
}

/** 挑一个中文音色；挑不到就交给浏览器按 `lang` 自己选。 */
function pickVoice(synth: SpeechSynthesis): SpeechSynthesisVoice | null {
  const voices = synth.getVoices()
  return (
    voices.find((voice) => voice.lang.replace('_', '-').toLowerCase().startsWith('zh-cn')) ??
    voices.find((voice) => voice.lang.replace('_', '-').toLowerCase().startsWith('zh')) ??
    null
  )
}

/**
 * 解锁语音引擎：iOS 只允许在**用户手势里**第一次发声 —— 手势之外直接 `speak()` 会被静默忽略
 * （这正是「开着喇叭却不念」的常见原因）。点开关本身就是手势，趁那个时候用一段听不见的
 * 空文本把引擎点着，之后 SSE 回包再念就正常了。
 */
export function unlockSpeech(): void {
  if (!speechSupported()) return
  const warmUp = new SpeechSynthesisUtterance(' ')
  warmUp.volume = 0
  warmUp.lang = 'zh-CN'
  window.speechSynthesis.speak(warmUp)
}

/** 念一段 Markdown 文本（内部会先转成纯文本）。 */
export function speak(markdown: string): void {
  if (!speechSupported()) return
  const text = toSpeechText(markdown)
  if (!text) return
  const synth = window.speechSynthesis
  const parts = chunkText(text)

  const queue = () => {
    const voice = pickVoice(synth)
    synth.resume?.() // Chrome 会把队列挂起（尤其是切回前台之后），先唤醒再说
    for (const piece of parts) {
      const utterance = new SpeechSynthesisUtterance(piece)
      utterance.lang = 'zh-CN'
      if (voice) utterance.voice = voice
      utterance.rate = 1.05
      synth.speak(utterance)
    }
  }

  if (synth.speaking || synth.pending) {
    // 一次只念一条：新输出进来先打断旧的。但 iOS 上 `cancel()` 之后同一轮里立刻 `speak()`
    // 会被吞掉，所以丢到下一个任务里再念。
    synth.cancel()
    window.setTimeout(queue, 80)
  } else {
    queue()
  }
}

/** 停止朗读（关开关、离开页面时调用）。 */
export function stopSpeaking(): void {
  if (speechSupported()) window.speechSynthesis.cancel()
}

/**
 * 对话页用的朗读开关：`enabled` 是用户的选择（默认关闭），`speakIfEnabled()` 只在开着时出声。
 * 关掉开关、离开页面都会立刻停声，不留一个在后台念的尾巴。
 */
export function useSpeechOutput(): {
  supported: boolean
  enabled: boolean
  setEnabled: (enabled: boolean) => void
  speakIfEnabled: (markdown: string) => void
} {
  const [enabled, setEnabledState] = useState(readSpeechEnabled)
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled

  const supported = useMemo(speechSupported, [])

  // iOS 上首次 `getVoices()` 常常是空的，得等 `voiceschanged`；这里先热一下，讲的时候就能挑到中文音色。
  useEffect(() => {
    if (!supported) return
    const synth = window.speechSynthesis
    const warm = () => synth.getVoices()
    warm()
    synth.addEventListener?.('voiceschanged', warm)
    return () => {
      synth.removeEventListener?.('voiceschanged', warm)
      stopSpeaking()
    }
  }, [supported])

  const setEnabled = useCallback((value: boolean) => {
    setEnabledState(value)
    storeSpeechEnabled(value)
    // 开启的那一下是用户手势：借它把语音引擎解锁（iOS 必需）。
    if (value) unlockSpeech()
    else stopSpeaking()
  }, [])

  const speakIfEnabled = useCallback((markdown: string) => {
    if (enabledRef.current) speak(markdown)
  }, [])

  return { supported, enabled, setEnabled, speakIfEnabled }
}
