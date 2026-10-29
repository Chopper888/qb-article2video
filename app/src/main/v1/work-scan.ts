import { existsSync, lstatSync, readdirSync, readFileSync, statSync, type Dirent } from 'node:fs'
import { basename, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { unescapeMarkdown } from '../../shared/v1/title'
import { toTaskRow, type CleanupArticle, type CleanupPreview, type CleanupResult, type DiskUsageReal, type TaskFacts, type TaskRow } from '../../shared/v1/tasks'

// 工作文件夹扫描（任务书 APP2 3.3）：只读，不改任何文件。
// APP3：唯一会改文件的是 runCleanup（清理中间文件，移到回收站，删除动作由调用方传入）。
// 文章文件夹 = 工作文件夹下一层、含 00-输入/ 或 进度账本.json 的目录；_a2v_tmp、_a2v_import 和其他目录忽略。

// _a2v_import：引擎解压 zip、APP 放拖进来的多个文件的地方（APP4 / 约定 v5.5）
export const IGNORED_DIRS = new Set(['_a2v_tmp', '_a2v_import'])

const readJson = (file: string): Record<string, unknown> | null => {
  try {
    const value: unknown = JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, ''))
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
  } catch { return null }
}

const str = (value: unknown): string | null => typeof value === 'string' && value.trim() ? value.trim() : null
const num = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null

function isDir(path: string): boolean {
  try { return statSync(path).isDirectory() } catch { return false }
}

function isFile(path: string): boolean {
  try { return statSync(path).isFile() } catch { return false }
}

export function listArticleDirs(workRoot: string): string[] {
  let entries: Dirent[]
  try { entries = readdirSync(workRoot, { withFileTypes: true }) } catch { return [] }
  return entries.filter(entry => entry.isDirectory() && !IGNORED_DIRS.has(entry.name) && !entry.name.startsWith('.'))
    .map(entry => entry.name)
    .filter(name => isDir(join(workRoot, name, '00-输入')) || isFile(join(workRoot, name, '进度账本.json')))
    .sort()
}

export interface ReportSummary { reds: number | null; notices: number | null; verdict: 'pass' | 'review' | 'fail' | null }

export function readReportSummary(articleDir: string): ReportSummary {
  const report = readJson(join(articleDir, '10-报告', '检查结果.json'))
  if (!report) return { reds: null, notices: null, verdict: null }
  const items = Array.isArray(report.items) ? report.items as { level?: unknown }[] : null
  const verdict = report.verdict === 'pass' || report.verdict === 'review' || report.verdict === 'fail' ? report.verdict : null
  return { reds: items ? items.filter(item => item.level === 'red').length : null, notices: items ? items.filter(item => item.level === 'notice').length : null, verdict }
}

// APP4：显示时去掉 Markdown 转义（旧规则导入的文章，来源.json 里的原标题还带 \. \- \&），不改文件
export function readArticleTitle(articleDir: string): string | null {
  const source = readJson(join(articleDir, '00-输入', '来源.json'))
  const title = source ? str(source['原标题']) ?? str(source['原始标题']) ?? str(source['清洗后标题']) : null
  return title === null ? null : unescapeMarkdown(title)
}

// APP4：文章类型以账本为准，账本没写时看 S0 的结果 00-输入/类型判断.json
export function readContentType(articleDir: string, ledger: Record<string, unknown> | null = readJson(join(articleDir, '进度账本.json'))): 'theory' | 'screenshot' | null {
  const pick = (value: unknown): 'theory' | 'screenshot' | null => value === 'theory' || value === 'screenshot' ? value : null
  return pick(ledger?.content_type) ?? pick(readJson(join(articleDir, '00-输入', '类型判断.json'))?.content_type)
}

export function readTaskFacts(workRoot: string, article: string, active: boolean): TaskFacts {
  const dir = join(workRoot, article)
  const source = readJson(join(dir, '00-输入', '来源.json'))
  const ledger = readJson(join(dir, '进度账本.json'))
  const from = source ? str(source['来源']) : null
  const steps = Array.isArray(ledger?.steps)
    ? (ledger!.steps as Record<string, unknown>[]).filter(step => typeof step.step === 'string')
      .map(step => ({ step: step.step as string, status: str(step.status) ?? 'pending', ...(str(step.message) ? { message: str(step.message)! } : {}) }))
    : null
  const totals = ledger?.totals && typeof ledger.totals === 'object' ? ledger.totals as Record<string, unknown> : null
  const type = readContentType(dir, ledger)
  const report = readReportSummary(dir)
  return {
    article,
    title: readArticleTitle(dir),
    // APP4：本地来源有 本地 md 文件夹 / 本地 zip / 本地 md 文件
    source: from ? (from.includes('飞书') ? '飞书' : from.includes('本地') || from.includes('md') || from.includes('Markdown') ? 'Markdown' : null) : null,
    contentType: type,
    steps,
    costCny: totals ? num(totals.estimated_cny) : null,
    reds: report.reds,
    verdict: report.verdict,
    updatedAt: (ledger ? str(ledger.updated) : null) ?? mtimeIso(dir),
    active,
    // 只在"已完成"时用到：中间产物文件夹里除 cache/ 以外已经没东西了
    cleaned: steps !== null && cleanupTargets(dir).length === 0
  }
}

function mtimeIso(path: string): string | null {
  try { return statSync(path).mtime.toISOString() } catch { return null }
}

export function scanTasks(workRoot: string, activeArticle: string | null): TaskRow[] {
  return listArticleDirs(workRoot)
    .map(article => toTaskRow(readTaskFacts(workRoot, article, article === activeArticle)))
    .sort((a, b) => (b.updatedAt ?? '').localeCompare(a.updatedAt ?? ''))
}

// 目录大小（不跟随符号链接）
export function dirSize(path: string): number {
  let total = 0
  let entries: Dirent[]
  try { entries = readdirSync(path, { withFileTypes: true }) } catch { return 0 }
  for (const entry of entries) {
    const full = join(path, entry.name)
    if (entry.isDirectory()) total += dirSize(full)
    else if (entry.isFile()) { try { total += statSync(full).size } catch { /* 读不到不计 */ } }
  }
  return total
}

// 文章文件夹里的缓存目录（各步骤的 cache/，重做同一篇时可以省钱）
function cacheDirs(path: string, out: string[] = []): string[] {
  let entries: Dirent[]
  try { entries = readdirSync(path, { withFileTypes: true }) } catch { return out }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const full = join(path, entry.name)
    if (entry.name === 'cache') out.push(full); else cacheDirs(full, out)
  }
  return out
}

// 清理时保留的（固定规则）：00-输入、09-成片、10-报告、logs；所有步骤目录里的 cache/；
// 文章根目录下的文件（账本、来源等）。也就是：删掉中间产物子文件夹里除 cache/ 以外的内容。
export const KEEP_TOP = new Set(['00-输入', '09-成片', '10-报告', 'logs'])
export const CACHE_DIR = 'cache'
export const RUN_LOCK = '.run.lock'

export function intermediateDirs(articleDir: string): string[] {
  let entries: Dirent[]
  try { entries = readdirSync(articleDir, { withFileTypes: true }) } catch { return [] }
  // 只认真正的目录（符号链接不跟随）；隐藏目录不碰
  return entries.filter(entry => entry.isDirectory() && !KEEP_TOP.has(entry.name) && !entry.name.startsWith('.')).map(entry => join(articleDir, entry.name)).sort()
}

function containsCache(dir: string): boolean {
  let entries: Dirent[]
  try { entries = readdirSync(dir, { withFileTypes: true }) } catch { return false }
  return entries.some(entry => entry.isDirectory() && (entry.name === CACHE_DIR || containsCache(join(dir, entry.name))))
}

// 文章夹里要移走的东西：中间产物文件夹里没有 cache/ 的整个移走；有 cache/ 的往里走，除 cache/ 以外的逐项移走
export function cleanupTargets(articleDir: string): string[] {
  const out: string[] = []
  const collect = (dir: string): void => {
    if (!containsCache(dir)) { out.push(dir); return }
    let entries: Dirent[]
    try { entries = readdirSync(dir, { withFileTypes: true }) } catch { return }
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      const full = join(dir, entry.name)
      if (entry.isDirectory() && entry.name === CACHE_DIR) continue
      if (entry.isDirectory() && containsCache(full)) collect(full); else out.push(full)
    }
  }
  for (const dir of intermediateDirs(articleDir)) collect(dir)
  return out
}

// 安全检查：要移走的路径必须在这篇文章的中间产物文件夹里，不能是保留清单里的东西
export function isSafeCleanupTarget(articleDir: string, target: string): boolean {
  const rel = relative(resolve(articleDir), resolve(target))
  if (!rel || rel.startsWith('..') || isAbsolute(rel)) return false
  const parts = rel.split(sep)
  return !KEEP_TOP.has(parts[0]!) && !parts[0]!.startsWith('.') && !parts.includes(CACHE_DIR) && !parts.includes('..')
}

function within(root: string, path: string): boolean {
  const rel = relative(resolve(root), resolve(path))
  return !!rel && !rel.startsWith('..') && !isAbsolute(rel)
}

// 成片确实在成片保存位置里（文件存在、大于 0）：账本记的交付路径，或 <成片保存位置>/<文章>/<文章>.mp4
export function deliveredVideo(workRoot: string, outputDir: string, article: string): string | null {
  const ledger = readJson(join(workRoot, article, '进度账本.json'))
  const candidates = [str(ledger?.delivery_path), join(outputDir, article, `${article}.mp4`)]
  for (const file of candidates) {
    if (!file || !within(outputDir, file)) continue
    try { if (statSync(file).isFile() && statSync(file).size > 0) return file } catch { /* 没有 */ }
  }
  return null
}

const sizeOf = (path: string): number => {
  try { const info = statSync(path); return info.isDirectory() ? dirSize(path) : info.isFile() ? info.size : 0 } catch { return 0 }
}

const sizeWithoutCache = (dir: string): number => dirSize(dir) - cacheDirs(dir).reduce((sum, cache) => sum + dirSize(cache), 0)

export function diskUsage(workRoot: string, activeArticle: string | null): DiskUsageReal {
  let completed = 0, unfinished = 0, cache = 0
  for (const row of scanTasks(workRoot, activeArticle)) {
    const dir = join(workRoot, row.article)
    const caches = cacheDirs(dir).reduce((sum, item) => sum + dirSize(item), 0)
    cache += caches
    const rest = dirSize(dir) - caches
    if (row.state === 'done' || row.state === 'done_red') completed += rest; else unfinished += rest
  }
  return { completed, unfinished, cache, total: completed + unfinished + cache }
}

// 账本里有 imported 步骤（从样本导入，这类步骤没有 cache）：清理后再跑会重新调用付费服务，不列入可清理
export function hasImportedStep(articleDir: string): boolean {
  const ledger = readJson(join(articleDir, '进度账本.json'))
  if (!Array.isArray(ledger?.steps)) return false
  return (ledger!.steps as Record<string, unknown>[]).some(step => step && typeof step === 'object' && step.status === 'imported')
}

// 可以清理的文章（固定规则）：
//   - 账本显示已完成（每一步都 done / imported / skipped，没有 failed、running：最后一次运行退出码 0 或 1 才会这样；
//     引擎不在磁盘上单独记退出码），且 APP 里没有在跑、文章夹里没有 .run.lock；
//   - 账本里没有 imported 步骤（APP3W：这类文章清理后再跑要重新花钱）；
//   - 成片确实在成片保存位置里（文件存在、大于 0）。
// 只列出，不删除。
export function cleanupPreview(workRoot: string, activeArticle: string | null, outputDir: string): CleanupPreview {
  const articles: CleanupArticle[] = []
  for (const row of scanTasks(workRoot, activeArticle)) {
    if (row.state !== 'done' && row.state !== 'done_red') continue
    const dir = join(workRoot, row.article)
    if (existsSync(join(dir, RUN_LOCK)) || !deliveredVideo(workRoot, outputDir, row.article)) continue
    if (hasImportedStep(dir)) continue
    const items = cleanupTargets(dir).filter(path => isSafeCleanupTarget(dir, path)).map(path => ({ path, bytes: sizeOf(path) }))
    if (!items.length) continue
    articles.push({ article: row.article, title: row.title, bytes: items.reduce((sum, item) => sum + item.bytes, 0), items })
  }
  return { articles, totalBytes: articles.reduce((sum, item) => sum + item.bytes, 0) }
}

// 真正清理：只清理用户在确认框里确认过的文章，并且重新按规则判一遍（不信页面传来的路径）。
// 每一项都交给 trash（Electron shell.trashItem，移到回收站 / 废纸篓）；第一次失败就停下，其余不动。
export async function runCleanup(workRoot: string, activeArticle: string | null, outputDir: string, requested: string[],
  trash: (path: string) => Promise<void>, log: (line: string) => void = () => undefined): Promise<CleanupResult> {
  const plan = cleanupPreview(workRoot, activeArticle, outputDir).articles.filter(item => requested.includes(item.article))
  const result: CleanupResult = { ok: true, articles: [], trashed: 0, bytes: 0 }
  for (const article of plan) {
    const dir = join(workRoot, article.article)
    if (existsSync(join(dir, RUN_LOCK)) || article.article === activeArticle) continue
    for (const item of article.items) {
      if (!isSafeCleanupTarget(dir, item.path)) throw new Error(`清理目标不在允许范围：${item.path}`)
      try {
        await trash(item.path)
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error)
        log(`清理中间文件：${item.path} 没能移到回收站（${reason}），停止清理`)
        if (result.trashed && !result.articles.includes(article.article)) result.articles.push(article.article)
        return { ...result, ok: false, error: `没能移到回收站：${reason}。剩下的没有删除${result.trashed ? `；已移走的 ${result.trashed} 项可以在回收站找回` : ''}。` }
      }
      result.trashed += 1
      result.bytes += item.bytes
    }
    result.articles.push(article.article)
    log(`清理中间文件：${article.article} 移到回收站 ${article.items.length} 项，共 ${article.bytes} 字节`)
  }
  return result
}

// 成片库：成片保存位置下的 mp4（引擎交付为 <成片保存位置>/<文章>/<文章>.mp4；也收根目录下直接放的 mp4）
export function listVideos(outputDir: string): { file: string; article: string | null }[] {
  const out: { file: string; article: string | null }[] = []
  let entries: Dirent[]
  try { entries = readdirSync(outputDir, { withFileTypes: true }) } catch { return out }
  for (const entry of entries) {
    const full = join(outputDir, entry.name)
    if (entry.isFile() && /\.mp4$/i.test(entry.name)) out.push({ file: full, article: null })
    else if (entry.isDirectory() && !entry.name.startsWith('.')) {
      let inner: Dirent[]
      try { inner = readdirSync(full, { withFileTypes: true }) } catch { continue }
      for (const item of inner) if (item.isFile() && /\.mp4$/i.test(item.name)) out.push({ file: join(full, item.name), article: entry.name })
    }
  }
  return out
}

export function videoArticle(file: string, article: string | null): string {
  return article ?? basename(file).replace(/\.mp4$/i, '')
}

// 1.0.1（R101 J）：删除任务——把这篇文章的工作文件夹整个移到回收站（废纸篓），不永久删除。
// 正在出片的（APP 里在跑，或文章夹里有 .run.lock）不能删。只认工作文件夹下一层的文章文件夹。
export type DeleteTaskCheck = { ok: true; article: string; path: string } | { ok: false; article: string; error: string }

export function deleteTaskCheck(workRoot: string, activeArticle: string | null, article: string): DeleteTaskCheck {
  const dir = join(workRoot, article)
  const rel = relative(resolve(workRoot), resolve(dir))
  if (!rel || rel.startsWith('..') || isAbsolute(rel) || rel.split(sep).length !== 1) return { ok: false, article, error: '文章名不对' }
  let isDir = false
  try { isDir = lstatSync(dir).isDirectory() } catch { /* 不在了 */ }
  if (!isDir) return { ok: false, article, error: '这个任务的文件夹已经不在了' }
  if (article === activeArticle || existsSync(join(dir, RUN_LOCK))) return { ok: false, article, error: '这个任务正在出片，不能删除；先停止再删' }
  return { ok: true, article, path: dir }
}

export async function deleteTask(workRoot: string, activeArticle: string | null, article: string,
  trash: (path: string) => Promise<void>, log: (line: string) => void = () => undefined): Promise<DeleteTaskCheck> {
  const check = deleteTaskCheck(workRoot, activeArticle, article)
  if (!check.ok) return check
  try {
    await trash(check.path)
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error)
    log(`删除任务：${check.path} 没能移到回收站（${reason}），没有删除`)
    return { ok: false, article, error: `没能移到回收站：${reason}。文件夹没有删除。` }
  }
  log(`删除任务：${article} 的工作文件夹已移到回收站：${check.path}`)
  return check
}
