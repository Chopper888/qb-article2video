// 版本比较、发布说明 → 纯文字（R120M2 起）。R121M5：检查 / 下载 / 安装改由 Main 的自动更新服务（auto-updater.ts）负责，
// 原来只做提醒的 evaluateRelease / findQuarkLink 已删除。

// 启动后约 10 秒再自动检查（不挡界面）
export const AUTO_CHECK_DELAY_MS = 10_000

export interface SemVer { major: number; minor: number; patch: number; pre: (string | number)[] }

// 认 "1.2.1" / "v1.2.1" / "V1.2.1-beta.2" / "1.2.1+build"；其他一律 null。
export function parseSemver(raw: unknown): SemVer | null {
  if (typeof raw !== 'string') return null
  const m = /^[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z.-]+)?$/.exec(raw.trim())
  if (!m) return null
  const pre = m[4] ? m[4].split('.').map(id => /^\d+$/.test(id) ? Number(id) : id) : []
  return { major: Number(m[1]), minor: Number(m[2]), patch: Number(m[3]), pre }
}

export function versionText(v: SemVer): string {
  return `${v.major}.${v.minor}.${v.patch}${v.pre.length ? '-' + v.pre.join('.') : ''}`
}

// 语义化版本优先级：a > b 返回正数，相等 0，a < b 负数。预发布号低于正式版（1.2.1-beta < 1.2.1）。
export function compareSemver(a: SemVer, b: SemVer): number {
  for (const k of ['major', 'minor', 'patch'] as const) if (a[k] !== b[k]) return a[k] - b[k]
  if (!a.pre.length || !b.pre.length) return (a.pre.length ? -1 : 0) - (b.pre.length ? -1 : 0)
  for (let i = 0; i < Math.max(a.pre.length, b.pre.length); i++) {
    const x = a.pre[i]
    const y = b.pre[i]
    if (x === undefined) return -1
    if (y === undefined) return 1
    if (x === y) continue
    if (typeof x === 'number' && typeof y === 'number') return x - y
    if (typeof x === 'number') return -1
    if (typeof y === 'number') return 1
    return x < y ? -1 : 1
  }
  return 0
}

const URL_RE = /https?:\/\/[^\s<>"'()\[\]{}，。；、）】]+/g

// 一行 Markdown → 纯文本：去标题/列表/引用符号、粗斜体、行内代码、HTML 标签；[文字](链接) 只留文字；裸链接一律去掉。
export function stripMarkdown(line: string): string {
  let s = line
  s = s.replace(/<[^>]*>/g, '')
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
  s = s.replace(URL_RE, '')
  s = s.replace(/^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+(\[[ xX]\]\s+)?|\d+[.)]\s+)+/, '')
  s = s.replace(/(\*\*|__|~~|`|\*)/g, '')
  return s.replace(/\s+/g, ' ').trim()
}

// 发布说明前 3 行（去掉 Markdown 符号后非空的行；分隔线、代码块围栏不算）
export function notesPreview(body: string, lines = 3): string[] {
  const out: string[] = []
  for (const raw of body.replace(/\r\n?/g, '\n').split('\n')) {
    if (/^\s*(```|~~~|[-*_]{3,}\s*$|\|?\s*:?-{3,})/.test(raw)) continue
    const text = stripMarkdown(raw)
    if (!text) continue
    out.push(text.length > 120 ? text.slice(0, 119) + '…' : text)
    if (out.length >= lines) break
  }
  return out
}
