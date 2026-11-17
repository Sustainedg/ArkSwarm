/**
 * 可点开放大的图片：平时按缩略图显示，点一下铺满全屏看原图。附件预览、气泡嵌图、
 * Markdown 正文里的图片都复用它，交互与样式两端一致（避免「图片能看但点不开」）。
 */
import { useEffect, useState } from 'react'

export function ZoomableImage({
  src,
  alt,
  className,
  onError,
}: {
  src: string
  alt?: string
  className?: string
  /** 图片取不到时交给调用方决定如何退化（例如附件改成文件名胶囊）。 */
  onError?: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        className={`zoomable${className ? ` ${className}` : ''}`}
        onClick={() => setOpen(true)}
        aria-label={alt ? `放大查看：${alt}` : '放大查看图片'}
      >
        <img src={src} alt={alt ?? ''} loading="lazy" onError={onError} />
      </button>
      {open && <ImageLightbox src={src} alt={alt} onClose={() => setOpen(false)} />}
    </>
  )
}

/** 全屏看原图：点图片外的蒙层或按 Esc 关闭。 */
export function ImageLightbox({
  src,
  alt,
  onClose,
}: {
  src: string
  alt?: string
  onClose: () => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    // 打开时锁住背景滚动，关掉恢复 —— 手机上否则会跟着手指把后面的对话一起滚。
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose])

  return (
    <div
      className="img-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label="查看图片"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <img src={src} alt={alt ?? ''} />
      <button type="button" className="img-lightbox-close" onClick={onClose} aria-label="关闭">
        ✕
      </button>
    </div>
  )
}
