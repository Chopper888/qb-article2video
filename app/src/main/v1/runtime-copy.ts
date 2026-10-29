import { existsSync, type Dirent } from 'node:fs'
import { copyFile, lstat, mkdir, readdir, rename, rm, stat } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import type { RuntimePrep } from '../../shared/v1/types'

// 安装版首次出片前：把 Runtime Pack 里的 runtime（node_modules + 字体）复制到短路径
// %LOCALAPPDATA%\wzcp\rt-<版本>（Windows 路径长度限制，旧流程由 prepare_sample --with-runtime 完成）。
// 先复制到 <目标>.tmp，完整后改名。
// APP3：逐个文件复制并报进度（按字节算百分比）；上次没复制完留下的 <目标>.tmp 是这里自己写的，开始前删掉重来，
// 所以失败后点"重试"能接着用。目标目录存在但不完整（不是这里改名出来的）仍然报错，不自动删除。

export type RuntimeCopyState = RuntimePrep

export function runtimeComplete(dir: string, exists: (path: string) => boolean = existsSync): boolean {
  return exists(join(dir, 'node_modules', 'remotion')) && exists(join(dir, 'fonts', 'LXGWWenKai-Regular.ttf'))
}

export interface CopyFs {
  copyFile: typeof copyFile
  lstat: typeof lstat
  mkdir: typeof mkdir
  readdir: typeof readdir
  rename: typeof rename
  rm: typeof rm
  stat: typeof stat
  exists: (path: string) => boolean
}

const realFs: CopyFs = { copyFile, lstat, mkdir, readdir, rename, rm, stat, exists: existsSync }

export interface CopyProgress { doneFiles: number; totalFiles: number; doneBytes: number; totalBytes: number }

async function listTree(root: string, fs: CopyFs): Promise<{ dirs: string[]; files: { rel: string; bytes: number }[] }> {
  const dirs: string[] = []
  const files: { rel: string; bytes: number }[] = []
  const walk = async (rel: string): Promise<void> => {
    const entries = await fs.readdir(join(root, rel), { withFileTypes: true }) as Dirent[]
    for (const entry of entries) {
      const child = rel ? join(rel, entry.name) : entry.name
      if (entry.isDirectory()) { dirs.push(child); await walk(child) }
      else if (entry.isFile()) files.push({ rel: child, bytes: (await fs.stat(join(root, child))).size })
      else throw new Error(`运行环境里有不支持的文件类型：${child}`)
    }
  }
  await walk('')
  return { dirs, files }
}

export async function ensureRuntimeCopy(source: string, target: string,
  options: { onProgress?: (progress: CopyProgress) => void; fs?: CopyFs; concurrency?: number; log?: (line: string) => void } = {}): Promise<RuntimeCopyState> {
  const fs = options.fs ?? realFs
  if (runtimeComplete(target, fs.exists)) return { status: 'ready', dir: target }
  if (fs.exists(target)) return { status: 'failed', error: `运行环境目录不完整：${target}（请删除后重开 APP）` }
  const temp = `${target}.tmp`
  try {
    try {
      await fs.lstat(temp)
      await fs.rm(temp, { recursive: true, force: true })
      options.log?.(`运行环境：删除了上次没复制完的 ${basename(temp)}，重新复制`)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
    const tree = await listTree(source, fs)
    const progress: CopyProgress = { doneFiles: 0, totalFiles: tree.files.length, doneBytes: 0, totalBytes: tree.files.reduce((sum, file) => sum + file.bytes, 0) }
    options.onProgress?.({ ...progress })
    await fs.mkdir(temp, { recursive: true })
    for (const dir of tree.dirs) await fs.mkdir(join(temp, dir), { recursive: true })
    let next = 0
    const worker = async (): Promise<void> => {
      while (next < tree.files.length) {
        const file = tree.files[next++]!
        await fs.copyFile(join(source, file.rel), join(temp, file.rel))
        progress.doneFiles += 1
        progress.doneBytes += file.bytes
        options.onProgress?.({ ...progress })
      }
    }
    await Promise.all(Array.from({ length: Math.max(1, options.concurrency ?? 8) }, worker))
    if (!runtimeComplete(temp, fs.exists)) return { status: 'failed', error: '运行环境复制后不完整' }
    await fs.rename(temp, target)
    return { status: 'ready', dir: target }
  } catch (error) {
    return { status: 'failed', error: `复制运行环境失败：${error instanceof Error ? error.message : String(error)}` }
  }
}

export function progressPercent(progress: CopyProgress): number {
  if (progress.totalBytes > 0) return Math.min(100, Math.floor((progress.doneBytes / progress.totalBytes) * 100))
  return progress.totalFiles > 0 ? Math.min(100, Math.floor((progress.doneFiles / progress.totalFiles) * 100)) : 0
}

// ---- APP3：清理旧版本的运行环境 ----
// 当前版本准备好之后，删除 wzcp 这一层里名字形如 rt-* 的其他目录（含复制中断留下的 *.tmp）。
// 只看这一层、只认目录（符号链接不跟随、不删）；正在使用的不删；删除失败只记日志。
// 先改名为 <名字>.deleting-<随机> 再删：Windows 上目录里有文件被进程占用时改名会失败，整个目录原样保留；
// 改名后删到一半失败的，名字仍是 rt-*，下次启动接着删。

export interface OldRuntimeCleanup { removed: { name: string; bytes: number }[]; skipped: { name: string; reason: string }[] }

async function treeBytes(path: string, fs: CopyFs): Promise<number> {
  let total = 0
  let entries: Dirent[]
  try { entries = await fs.readdir(path, { withFileTypes: true }) as Dirent[] } catch { return 0 }
  for (const entry of entries) {
    const full = join(path, entry.name)
    if (entry.isDirectory()) total += await treeBytes(full, fs)
    else if (entry.isFile()) { try { total += (await fs.stat(full)).size } catch { /* 读不到不计 */ } }
  }
  return total
}

export async function cleanupOldRuntimes(current: string,
  options: { inUse?: (dir: string) => boolean; log?: (line: string) => void; fs?: CopyFs } = {}): Promise<OldRuntimeCleanup> {
  const fs = options.fs ?? realFs
  const log = options.log ?? (() => undefined)
  const root = dirname(current)
  const keep = basename(current)
  const result: OldRuntimeCleanup = { removed: [], skipped: [] }
  let entries: Dirent[]
  try { entries = await fs.readdir(root, { withFileTypes: true }) as Dirent[] } catch (error) {
    log(`清理旧运行环境：读不了 ${root}（${error instanceof Error ? error.message : String(error)}）`)
    return result
  }
  for (const entry of entries) {
    if (!entry.isDirectory() || !/^rt-./.test(entry.name) || entry.name === keep) continue
    const full = join(root, entry.name)
    if (options.inUse?.(full)) {
      result.skipped.push({ name: entry.name, reason: '正在使用' })
      log(`清理旧运行环境：${entry.name} 正在使用，不删`)
      continue
    }
    const bytes = await treeBytes(full, fs)
    // 已经是改过名的（上次删到一半）直接删；否则换一个不会重名的名字
    const doomed = entry.name.includes('.deleting') ? full : `${full}.deleting-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
    try {
      if (doomed !== full) await fs.rename(full, doomed)
    } catch (error) {
      result.skipped.push({ name: entry.name, reason: error instanceof Error ? error.message : String(error) })
      log(`清理旧运行环境：${entry.name} 没删（可能正在使用：${error instanceof Error ? error.message : String(error)}）`)
      continue
    }
    try {
      await fs.rm(doomed, { recursive: true, force: true })
      result.removed.push({ name: entry.name, bytes })
      log(`清理旧运行环境：已删除 ${entry.name}（${(bytes / 1024 / 1024).toFixed(1)} MB）`)
    } catch (error) {
      result.skipped.push({ name: entry.name, reason: error instanceof Error ? error.message : String(error) })
      log(`清理旧运行环境：${entry.name} 删除失败（${error instanceof Error ? error.message : String(error)}），下次启动再试`)
    }
  }
  return result
}

// ---- APP3：首次启动准备运行环境（状态 + 进度 + 重试），主进程里一个实例 ----

export class RuntimePreparer {
  private state: RuntimePrep = { status: 'not_needed' }
  private listeners = new Set<(state: RuntimePrep) => void>()
  private running: Promise<RuntimePrep> | null = null
  private lastEmit = 0

  constructor(private readonly options: {
    source: string
    target: string
    onReady(dir: string): void
    inUse?: (dir: string) => boolean
    log?: (line: string) => void
    fs?: CopyFs
    now?: () => number
  }) {}

  get(): RuntimePrep { return this.state }

  subscribe(listener: (state: RuntimePrep) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private set(state: RuntimePrep, force = true): void {
    const now = (this.options.now ?? Date.now)()
    this.state = state
    // 复制中的进度最多每 200 毫秒推一次
    if (!force && now - this.lastEmit < 200) return
    this.lastEmit = now
    for (const listener of this.listeners) listener(state)
  }

  start(): Promise<RuntimePrep> {
    if (this.running) return this.running
    if (this.state.status === 'ready') return Promise.resolve(this.state)
    const log = this.options.log ?? (() => undefined)
    const started = (this.options.now ?? Date.now)()
    this.set({ status: 'copying', percent: 0, doneFiles: 0, totalFiles: 0 })
    this.running = ensureRuntimeCopy(this.options.source, this.options.target, {
      fs: this.options.fs, log,
      onProgress: progress => this.set({ status: 'copying', percent: progressPercent(progress), doneFiles: progress.doneFiles, totalFiles: progress.totalFiles },
        progress.doneFiles === 0 || progress.doneFiles === progress.totalFiles)
    }).then(async state => {
      this.running = null
      if (state.status === 'ready') {
        log(`运行环境：就绪（${state.dir}，用时 ${(((this.options.now ?? Date.now)() - started) / 1000).toFixed(1)} 秒）`)
        this.options.onReady(state.dir)
        this.set(state)
        await cleanupOldRuntimes(state.dir, { inUse: this.options.inUse, log, fs: this.options.fs })
      } else {
        log(`运行环境：${state.status === 'failed' ? state.error : state.status}`)
        this.set(state)
      }
      return state
    })
    return this.running
  }

  retry(): Promise<RuntimePrep> {
    return this.state.status === 'failed' ? this.start() : (this.running ?? Promise.resolve(this.state))
  }
}
