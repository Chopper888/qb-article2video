import { unescapeMarkdown } from './title'

// 费用确认页左侧的"章节"：读导入后 00-输入/文章.md 的一级、二级标题（只读）。
// 代码块里的 # 不算；标题较多时只给前 limit 个，total 是总数。

export interface Chapter { level: 1 | 2; title: string }
export interface ChapterList { items: Chapter[]; total: number }

export const CHAPTER_LIMIT = 30

export function parseChapters(markdown: string, limit = CHAPTER_LIMIT): ChapterList {
  const items: Chapter[] = []
  let fence: string | null = null
  for (const raw of markdown.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = raw.trimEnd()
    const fenceMatch = /^\s{0,3}(`{3,}|~{3,})/.exec(line)
    if (fenceMatch) {
      const mark = fenceMatch[1]![0]!
      if (fence === null) fence = mark
      else if (fence === mark) fence = null
      continue
    }
    if (fence !== null) continue
    const heading = /^\s{0,3}(#{1,2})\s+(.+?)\s*#*\s*$/.exec(line)
    if (!heading) continue
    // 去掉强调符号和 Markdown 转义（飞书导出的标题常见 3\.1\.2、\&）
    const title = unescapeMarkdown(heading[2]!).replace(/[*_`]/g, '').trim()
    if (title) items.push({ level: heading[1]!.length as 1 | 2, title })
  }
  return { items: items.slice(0, limit), total: items.length }
}
