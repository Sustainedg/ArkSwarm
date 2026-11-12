/**
 * 语音输入（Web Speech API）：把说的话转成文字，填进对话输入框。
 *
 * 这套 API 不在 TS 标准库的 DOM 类型里（iOS Safari 至今只有 `webkit` 前缀版），
 * 所以按用到的字段做最小声明。不支持时 `speechRecognitionCtor()` 返回 `null`：
 * 麦克风按钮**仍然显示**（藏起来用户只会以为功能没了），只是点下去给一句中文说明 ——
 * 独立应用（加到主屏）里 iOS 常常没有这个构造函数，那时还能用系统键盘自带的听写。
 */

/** 一条识别候选。 */
interface SpeechResult {
  readonly transcript: string
  readonly isFinal: boolean
}

export interface SpeechResultEvent {
  readonly resultIndex: number
  readonly results: ArrayLike<ArrayLike<SpeechResult> & { isFinal: boolean }>
}

/** 识别器实例：只声明本项目用到的成员。 */
export interface SpeechRecognizer {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: SpeechResultEvent) => void) | null
  onerror: ((event: { error?: string }) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
  abort(): void
}

interface SpeechRecognizerCtor {
  new (): SpeechRecognizer
}

interface SpeechWindow {
  SpeechRecognition?: SpeechRecognizerCtor
  webkitSpeechRecognition?: SpeechRecognizerCtor
}

/** 当前浏览器支持的识别器构造器；不支持返回 `null`。 */
export function speechRecognitionCtor(): SpeechRecognizerCtor | null {
  const scope = window as unknown as SpeechWindow
  return scope.SpeechRecognition ?? scope.webkitSpeechRecognition ?? null
}

/** 取这一轮已经定稿（`isFinal`）的文字，拼成可直接追加到草稿的字符串。 */
export function finalTranscript(event: SpeechResultEvent): string {
  let text = ''
  for (let index = event.resultIndex; index < event.results.length; index += 1) {
    const result = event.results[index]
    if (result?.isFinal) text += result[0]?.transcript ?? ''
  }
  return text
}

/** 识别失败的中文提示：说清「哪里不对、怎么办」。 */
export function describeSpeechError(code: string | undefined): string {
  switch (code) {
    case 'not-allowed':
    case 'service-not-allowed':
      return '麦克风权限被拒绝：请在浏览器设置里允许本站使用麦克风'
    case 'no-speech':
      return '没有听到声音，请再说一次'
    case 'audio-capture':
      return '找不到可用的麦克风'
    case 'network':
      return '语音识别需要联网，请检查网络后重试'
    case 'aborted':
      return '语音输入已取消'
    default:
      return '语音识别失败，请重试（也可以直接用键盘上的听写）'
  }
}
