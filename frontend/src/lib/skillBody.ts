/**
 * 技能正文的展示处理。
 *
 * ``SKILL.md`` 顶部通常有一段 YAML frontmatter（name / description / version…）。
 * 这些字段已经解析进技能记录、在详情页里单独展示，正文再铺一遍只会变成一串粗体段落，
 * 所以在渲染 Markdown 前先把它摘掉。
 */
export function stripFrontmatter(text: string): string {
  if (!text.startsWith('---')) return text
  const match = /^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/.exec(text)
  return match ? text.slice(match[0].length).trimStart() : text
}
