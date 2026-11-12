import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, win32 } from 'node:path'
import { parseSemver, versionText } from '../../shared/v1/update-check'
import { DEFAULT_COMFYUI_URL, MOSS_STYLES, type ImageBackend, type KeyCheckRecord, type MossStyle, type V1Prefs } from '../../shared/v1/types'

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
  return { onboarded: false, workDir: dirs.workDir, outputDir: dirs.outputDir, maxCostCny: 50, theme: 'dark',
    imageBackend: 'gpt', mossStyle: 'S0', comfyuiUrl: DEFAULT_COMFYUI_URL, mossFallbackGpt: true,
    autoCheckUpdate: true, ignoredUpdateVersion: null, lastKeyCheck: null }
}

// R120M：出图设置的取值校验（不认识的值一律回到默认）
const BACKENDS = new Set<ImageBackend>(['gpt', 'moss_local', 'moss_rh'])
const STYLES = new Set<MossStyle>(MOSS_STYLES.map(style => style.id))
export function cleanBackend(value: unknown): ImageBackend | null { return BACKENDS.has(value as ImageBackend) ? value as ImageBackend : null }
// R121M：旧版的"RH 同时出图数"（mossRhConcurrency）已删除；旧设置文件里的这个字段读的时候忽略、下次保存时丢掉。
export function cleanStyle(value: unknown): MossStyle | null { return STYLES.has(value as MossStyle) ? value as MossStyle : null }
// ComfyUI 地址：只收 http(s)://主机[:端口][/路径]，去掉首尾空白和末尾斜杠；空串回到默认地址；不合法返回 null（保持原值）
export function cleanComfyUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  const v = value.trim().replace(/\/+$/, '')
  if (!v) return DEFAULT_COMFYUI_URL
  try {
    const url = new URL(v)
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || !url.hostname || url.username || url.password) return null
    return v.length <= 512 ? v : null
  } catch { return null }
}

// R120M2：忽略的版本号只收合法的语义化版本（存成不带 v 的形式）；其他一律落空
export function cleanVersion(value: unknown): string | null {
  const v = parseSemver(value)
  return v ? versionText(v) : null
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
    const backend = cleanBackend(patch.imageBackend)
    if (backend) next.imageBackend = backend
    const style = cleanStyle(patch.mossStyle)
    if (style) next.mossStyle = style
    if (patch.comfyuiUrl !== undefined) { const url = cleanComfyUrl(patch.comfyuiUrl); if (url) next.comfyuiUrl = url }
    if (typeof patch.mossFallbackGpt === 'boolean') next.mossFallbackGpt = patch.mossFallbackGpt
    if (typeof patch.autoCheckUpdate === 'boolean') next.autoCheckUpdate = patch.autoCheckUpdate
    if (patch.ignoredUpdateVersion !== undefined) next.ignoredUpdateVersion = cleanVersion(patch.ignoredUpdateVersion)
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
        imageBackend: cleanBackend(parsed.imageBackend) ?? d.imageBackend,
        mossStyle: cleanStyle(parsed.mossStyle) ?? d.mossStyle,
        comfyuiUrl: cleanComfyUrl(parsed.comfyuiUrl) ?? d.comfyuiUrl,
        mossFallbackGpt: typeof parsed.mossFallbackGpt === 'boolean' ? parsed.mossFallbackGpt : d.mossFallbackGpt,
        autoCheckUpdate: typeof parsed.autoCheckUpdate === 'boolean' ? parsed.autoCheckUpdate : d.autoCheckUpdate,
        ignoredUpdateVersion: cleanVersion(parsed.ignoredUpdateVersion) ?? d.ignoredUpdateVersion,
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
