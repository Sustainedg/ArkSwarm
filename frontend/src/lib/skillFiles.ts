/**
 * 技能压缩包的本地校验 / 读取与浏览器下载（两端共用）。
 *
 * 上传走 base64（与附件、头像同一套做法），因此这里只负责把 `File` 读成
 * 不带 `data:` 前缀的字符串；后端仍然会自己校验体积与压缩包结构。
 */

/** 单个技能压缩包上限，与后端 `app/skillhub.py:MAX_ARCHIVE_BYTES` 对齐。 */
export const MAX_SKILL_ARCHIVE_BYTES = 20 * 1024 * 1024

/** 上传前的本地校验：返回给用户看的中文原因，通过时返回 `null`。 */
export function validateSkillArchive(file: File): string | null {
  if (!file.name.toLowerCase().endsWith('.zip')) return '只支持 .zip 压缩包'
  if (file.size === 0) return '压缩包内容为空'
  if (file.size > MAX_SKILL_ARCHIVE_BYTES) {
    return `压缩包不能超过 ${MAX_SKILL_ARCHIVE_BYTES / 1024 / 1024} MB`
  }
  return null
}

/** 把文件读成 base64（不带 `data:` 前缀，后端只解 base64 本体）。 */
export function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : ''
      const comma = result.indexOf(',')
      if (comma < 0) {
        reject(new Error('读取压缩包失败'))
        return
      }
      resolve(result.slice(comma + 1))
    }
    reader.onerror = () => reject(new Error('读取压缩包失败'))
    reader.readAsDataURL(file)
  })
}

/** 触发一次浏览器下载：Blob → 临时 object URL → `<a download>`。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 立刻 revoke 在部分浏览器上会打断下载，延后回收。
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}
