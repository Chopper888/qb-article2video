import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, win32 } from 'node:path'
import type { KeyCheckRecord, V1Prefs } from '../../shared/v1/types'

// 1.0 界面自己的设置（与旧 app-state-v1.json 分开存，不改旧任务数据）。

export interface DefaultDirs { workDir: string; outputDir: string }

// 默认放在用户文件夹下的「文章成片\工作 / 成片」
export function defaultDirs(platform: NodeJS.Platform = process.platform, home: string = homedir()): DefaultDirs {
  if (platform === 'win32') {
    const base = win32.join(home, '文章成片')
    return { workDir: win32.join(base, '工作'), outputDir: win32.join(base, '成片') }
  }
  const base = join(home, '文章成片')
  return { workDir: join(base, '工作'), outputDir: join(base, '成片') }
}

export function defaultPrefs(dirs: DefaultDirs = defaultDirs()): V1Prefs {
  return { onboarded: false, workDir: dirs.workDir, outputDir: dirs.outputDir, maxCostCny: 50, theme: 'dark', lastKeyCheck: null }
}

const THEMES = new Set(['dark', 'light'])

export class PrefsStore {
  private prefs: V1Prefs
  // APP4：设置变了就通知（Main 推给页面，确认页、成片库、任务列表立刻用新值）
  private readonly listeners = new Set<(prefs: V1Prefs) => void>()

  constructor(private readonly file: string, private readonly defaults: V1Prefs = defaultPrefs()) {
    this.prefs = this.load()
  }

  get(): V1Prefs { return structuredClone(this.prefs) }

  subscribe(listener: (prefs: V1Prefs) => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  private changed(): void {
    for (const listener of this.listeners) { try { listener(this.get()) } catch { /* 通知失败不影响保存 */ } }
  }

  update(patch: Partial<V1Prefs>): V1Prefs {
    const next = { ...this.prefs }
    if (typeof patch.onboarded === 'boolean') next.onboarded = patch.onboarded
    if (typeof patch.workDir === 'string' && patch.workDir.trim()) next.workDir = patch.workDir.trim()
    if (typeof patch.outputDir === 'string' && patch.outputDir.trim()) next.outputDir = patch.outputDir.trim()
    if (typeof patch.maxCostCny === 'number' && Number.isFinite(patch.maxCostCny) && patch.maxCostCny > 0 && patch.maxCostCny <= 10000) {
      next.maxCostCny = Math.round(patch.maxCostCny * 100) / 100
    }
    if (typeof patch.theme === 'string' && THEMES.has(patch.theme)) next.theme = patch.theme
    this.prefs = next
    this.save()
    this.changed()
    return this.get()
  }

  // 检测结果只存状态和时间，不存余额
  recordKeyCheck(record: KeyCheckRecord): void {
    this.prefs = { ...this.prefs, lastKeyCheck: record }
    this.save()
    this.changed()
  }

  // APP4：只认已知的字段。文件里残留的其他字段（例如旧版本或手改留下的 noPaid）一律不读、下次保存时丢掉
  private load(): V1Prefs {
    try {
      const parsed = JSON.parse(readFileSync(this.file, 'utf8')) as Partial<V1Prefs>
      const d = this.defaults
      const text = (value: unknown, fallback: string): string => typeof value === 'string' && value.trim() ? value.trim() : fallback
      const cost = parsed.maxCostCny
      return {
        onboarded: typeof parsed.onboarded === 'boolean' ? parsed.onboarded : d.onboarded,
        workDir: text(parsed.workDir, d.workDir),
        outputDir: text(parsed.outputDir, d.outputDir),
        maxCostCny: typeof cost === 'number' && Number.isFinite(cost) && cost > 0 && cost <= 10000 ? cost : d.maxCostCny,
        theme: THEMES.has(parsed.theme as string) ? parsed.theme as V1Prefs['theme'] : d.theme,
        lastKeyCheck: parsed.lastKeyCheck && typeof parsed.lastKeyCheck === 'object' ? parsed.lastKeyCheck : d.lastKeyCheck
      }
    } catch {
      return structuredClone(this.defaults)
    }
  }

  private save(): void {
    mkdirSync(dirname(this.file), { recursive: true })
    const temp = `${this.file}.tmp`
    writeFileSync(temp, JSON.stringify(this.prefs, null, 2), 'utf8')
    renameSync(temp, this.file)
  }
}
