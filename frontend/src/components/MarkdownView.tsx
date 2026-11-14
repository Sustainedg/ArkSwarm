import {
  Children,
  isValidElement,
  useEffect,
  useState,
  type ReactNode,
} from 'react'
import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { resolveImageSrc, useMediaUrl } from '../lib/media'
import { ZoomableImage } from './ZoomableImage'

const remarkPlugins = [remarkGfm]

export function MarkdownView({ text }: { text: string }) {
  return (
    <div className="markdown-body">
      <Markdown remarkPlugins={remarkPlugins} components={markdownComponents} skipHtml>
        {text}
      </Markdown>
    </div>
  )
}

const markdownComponents: Components = {
  pre({ children }) {
    const codeChild = Children.toArray(children).find((child) => isValidElement(child))
    if (!isValidElement<{ className?: string; children?: ReactNode }>(codeChild)) {
      return <pre>{children}</pre>
    }

    const className = codeChild.props.className ?? ''
    const language = /language-([^\s]+)/.exec(className)?.[1] ?? ''
    const code = childrenToText(codeChild.props.children).replace(/\n$/, '')
    return <CodeBlock code={code} language={language} />
  },
  code({ className, children }) {
    return <code className={`md-inline-code${className ? ` ${className}` : ''}`}>{children}</code>
  },
  a({ href, children }) {
    const external = Boolean(href && /^https?:\/\//i.test(href))
    return (
      <a
        href={href}
        target={external ? '_blank' : undefined}
        rel={external ? 'noreferrer noopener' : undefined}
      >
        {children}
      </a>
    )
  },
  img({ src, alt }) {
    return <MarkdownImage src={typeof src === 'string' ? src : undefined} alt={alt} />
  },
}

/**
 * 正文里的图片（Markdown `![]()`）。外链直接加载；工作目录里的相对路径经页面的解析器
 * 换成后端只读出口的 URL —— 这样智能体只要在回复里给出一张图（哪怕只是工作目录里的路径），
 * 页面上就能真的显示出来，而不是一个裂图。解析不了时给一句可读的提示，不静默留白。
 */
function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const mediaUrl = useMediaUrl()
  if (!src) return null
  const resolved = resolveImageSrc(src, mediaUrl)
  if (!resolved) return <span className="md-image-missing">{alt || src}</span>
  return <ZoomableImage className="md-image" src={resolved} alt={alt} />
}

function CodeBlock({ code, language }: { code: string; language: string }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const lines = code ? code.split('\n').length : 0

  useEffect(() => {
    if (copyState === 'idle') return
    const timer = window.setTimeout(() => setCopyState('idle'), 1600)
    return () => window.clearTimeout(timer)
  }, [copyState])

  const copy = async () => {
    try {
      await copyText(code)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
  }

  return (
    <div className="md-code-block">
      <div className="md-code-head">
        <span className="md-code-lang">{language || '代码'}</span>
        {lines > 0 && <span className="md-code-lines">{lines} 行</span>}
        <button
          type="button"
          className="md-code-copy"
          onClick={() => void copy()}
          aria-label="复制代码"
        >
          {copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : '复制'}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  )
}

function childrenToText(value: ReactNode): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  if (Array.isArray(value)) return value.map(childrenToText).join('')
  if (isValidElement<{ children?: ReactNode }>(value)) return childrenToText(value.props.children)
  return ''
}

async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  const copied = document.execCommand('copy')
  textarea.remove()
  if (!copied) throw new Error('复制失败')
}
