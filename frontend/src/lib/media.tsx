/**
 * 工作目录文件的「取图」上下文：把 rel_path 变成浏览器能加载的 URL。
 *
 * 附件与智能体产出的图片都是工作目录里的普通文件，浏览器拿不到本地路径 —— 只有后端那个
 * 只读出口（`GET .../files/{rel_path}`）能取到。要在**气泡、输入区**这些深层的共用组件里嵌图，
 * 又不想把 conversation / run 的 id 一路 prop 钻下去，所以这里放一个轻量 context：
 * 页面在顶层提供一次「相对路径 → URL」的解析器，`MessageView` / `Composer` / `MarkdownView`
 * 按需取用。没有 provider 时解析器为 `null`，组件自动退化成「只显示文件名」。
 */
import { createContext, useContext, type ReactNode } from 'react'

/** 把工作目录里的相对路径变成可加载 URL；解析不了（无上下文）返回 null。 */
export type MediaUrlResolver = (relPath: string) => string | null

const MediaUrlContext = createContext<MediaUrlResolver | null>(null)

export function MediaUrlProvider({
  resolver,
  children,
}: {
  resolver: MediaUrlResolver | null
  children: ReactNode
}) {
  return <MediaUrlContext.Provider value={resolver}>{children}</MediaUrlContext.Provider>
}

/** 取当前页面的文件 URL 解析器；不在 provider 内时为 null。 */
export function useMediaUrl(): MediaUrlResolver | null {
  return useContext(MediaUrlContext)
}

const IMAGE_EXTENSION = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i

/** 按扩展名粗判一个文件名/路径是不是图片（能不能直接 `<img>`）。 */
export function isImagePath(nameOrPath: string): boolean {
  const clean = nameOrPath.split('?')[0].split('#')[0]
  return IMAGE_EXTENSION.test(clean)
}

/**
 * 一段可能指向工作目录的路径：绝对路径、相对路径或纯文件名。
 *
 * 扩展名后面只要求「不是字母数字或路径分隔符」，而不是枚举中文标点：智能体经常写成
 * `xxx.jpg（896×1152）`，全角括号若被当成路径的一部分或边界不匹配，整张图就会漏掉。
 */
const WORKSPACE_PATH = /(?:^|\s|`|[(（:：])((?:\/|\.{1,2}\/)?[^\s`<>"'()（）:：，,。；;]+\.[A-Za-z0-9]{1,8})(?![A-Za-z0-9_/\\])/g

/** 从一段文本里取出所有像图片路径的片段（去重，保持出现顺序）。 */
export function extractImagePaths(content: string): string[] {
  const found: string[] = []
  for (const match of content.matchAll(WORKSPACE_PATH)) {
    const value = match[1].trim()
    // 外链由 Markdown / 浏览器直接加载，不要当成工作目录里的文件画成「取不到」的胶囊。
    // `https://…` 可能被上一轮的 `/` 抢先命中，所以先看它前面的字符，而不是只看 value。
    const start = match.index + match[0].lastIndexOf(value)
    const before = content.slice(0, start)
    if (before.endsWith(':') || before.endsWith(':/')) continue
    // Markdown 图片 / 链接的地址（`![](…)` / `[…](…)`）由 Markdown 渲染器处理，
    // 这里再摘一遍会让同一张图在气泡里出现两次。
    if (before.endsWith('](') || before.endsWith(']（')) continue
    if (isImagePath(value) && !found.includes(value)) found.push(value)
  }
  // 「文件：a.jpg」与「位置：/app/.../a.jpg」同时出现时只留完整路径，避免同一张图画两遍。
  return found.filter(
    (path) =>
      path.includes('/') || !found.some((other) => other !== path && other.endsWith(`/${path}`)),
  )
}

/**
 * 把 Markdown / 正文里的图片地址解析成可加载的 URL。
 * - 外链 / `data:` / `blob:`：原样返回（本来就能加载）；
 * - 绝对路径 / 相对路径 / 纯文件名：原样交给页面解析器，再由后端只读出口校验
 *   它确实在当前工作目录内（容器内的绝对路径只要位于工作目录下也能直接取到）。
 */
export function resolveImageSrc(src: string, resolve: MediaUrlResolver | null): string | null {
  const value = src.trim()
  if (!value) return null
  if (/^(https?:|data:|blob:)/i.test(value)) return value
  return resolve ? resolve(value) : null
}

/**
 * 用户消息里的「📎 附件」清单：把它从正文里摘出来，正文与附件路径分开。
 *
 * 这段清单由后端 `app.attachments.compose_with_attachments` 写在用户消息正文末尾
 * （附件随消息发给智能体时，正文里要留一份路径）。渲染气泡时把它拆开，图片才画得成缩略图，
 * 而不是把一串路径当普通文字铺在气泡里。格式对不上就原样返回，不会误伤正常文本。
 */
export function splitAttachmentBlock(content: string): { text: string; paths: string[] } {
  const match = /\n*📎 附件（已存入工作目录，可直接读取）：\n((?:- [^\n]+\n?)+)/.exec(content)
  if (!match) return { text: content, paths: [] }
  const paths = match[1]
    .split('\n')
    .map((line) => line.replace(/^-\s*/, '').trim())
    .filter(Boolean)
  return { text: content.slice(0, match.index).trimEnd(), paths }
}

/**
 * `splitAttachmentBlock` 的逆向操作：把附件清单拼回正文末尾。
 *
 * 实时卡片（`live`）在消息落库、被重新拉回来之前就把它渲染出来了，手上只有正文与
 * `rel_path` 清单；这里拼成与后端 `app.attachments.compose_with_attachments` **逐字一致**
 * 的字符串，`MessageView` 才能用同一套解析把附件缩略图画进「你」的气泡里 ——
 * 否则刚发出去的图要等整轮回复跑完、重新拉库时才出现（正是「附件还留在输入框」的观感来源）。
 */
export function composeWithAttachments(content: string, relPaths: string[]): string {
  if (relPaths.length === 0) return content
  const block = `📎 附件（已存入工作目录，可直接读取）：\n${relPaths.map((path) => `- ${path}`).join('\n')}`
  const text = content.trim()
  return text ? `${text}\n\n${block}` : block
}
