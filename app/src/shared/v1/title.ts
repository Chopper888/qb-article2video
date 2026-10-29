// APP4：标题去掉 Markdown 反斜杠转义（飞书导出的 3\.2\.1 基础篇\-a\&b → 3.2.1 基础篇-a&b）。
// 只影响显示，不改文件。规则与引擎一致：反斜杠后面跟 ASCII 标点就是转义；行内代码里的不动。

const ESCAPE = /\\([!-/:-@[-`{-~])/g

export function unescapeMarkdown(text: string): string {
  let out = ''
  let pos = 0
  const span = /(`+)(?:.*?[^`])?\1(?!`)/g
  for (let m = span.exec(text); m; m = span.exec(text)) {
    out += text.slice(pos, m.index).replace(ESCAPE, '$1') + m[0]
    pos = m.index + m[0].length
  }
  return out + text.slice(pos).replace(ESCAPE, '$1')
}
