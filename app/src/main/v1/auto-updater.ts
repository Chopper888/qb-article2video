import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { cleanReleaseNotes, initialUpdateState, type UpdateFailStage, type UpdateState } from '../../shared/v1/auto-update'
import { GITHUB_RELEASES_URL, QUARK_DOWNLOAD_URL, isAllowedUpdateLink } from '../../shared/v1/links'
import { compareSemver, parseSemver, versionText } from '../../shared/v1/update-check'

// R121M5：APP 内自动更新（Main）。这里只管流程和状态，真正的检查 / 下载 / 校验 / 安装交给"更新后端"
// （安装版是 electron-updater，见 electron-updater-backend.ts；测试里是替身）。
//
// 规矩（任务书 §1）：
// - 启动后自动检查一次（设置里关了就不查）；连不上、没有新版本都不提示。
// - 不自动下载：用户点"立即更新"才下载。
// - 下载完不自动重启：等用户点"重启安装"，或者关闭软件时由后端自己安装（autoInstallOnAppQuit）。
// - 出片进行中不许"重启安装"（这里再拦一次，页面上按钮也置灰）；这里任何地方都不会主动退出 / 重启软件。
// - 下载失败 / 校验失败 / 安装失败 → failed，页面给两个手动下载地址。

export const FEED_ENV = 'A2V_UPDATE_FEED_URL'
export const CHECK_TIMEOUT_MS = 30_000
export const BUSY_POLL_MS = 1_000
export const HISTORY_MAX = 30

export interface BackendCheckResult { available: boolean; version: string; notes?: unknown }
export interface BackendProgress { percent: number; transferred: number; total: number }

export interface UpdaterBackend {
  check(): Promise<BackendCheckResult>
  download(onProgress: (progress: BackendProgress) => void): Promise<void>
  // 立刻退出并安装（装完重新打开）。只在用户点"重启安装"时调用
  quitAndInstall(): void
}

export interface AutoUpdateOptions {
  currentVersion: string
  // 懒加载：第一次检查时才装载（开发模式返回 null = 不支持）
  backend: () => Promise<UpdaterBackend | null> | UpdaterBackend | null
  autoCheck(): boolean
  ignoredVersion(): string | null
  setIgnoredVersion(version: string): void
  // 正在出片
  busy(): boolean
  log(line: string): void
  // 记住"已下载、等安装"的版本和最近一次检查结果（userData/update-state.json）；不给就只放内存
  stateFile?: string
  feedOverridden?: boolean
  now?: () => Date
  checkTimeoutMs?: number
  setInterval?: (fn: () => void, ms: number) => unknown
  clearInterval?: (timer: unknown) => void
}

// 诊断报告用：当前版本、检查时间、发现的版本、下载结果、失败原因
export interface UpdateDiagnostics {
  currentVersion: string
  feedOverridden: boolean
  autoCheck: boolean
  lastCheckedAt: string | null
  lastCheckResult: string | null
  foundVersion: string | null
  downloadResult: string | null
  failStage: UpdateFailStage | null
  failReason: string | null
  history: string[]
}

interface Persisted {
  // 已经下载、等着安装的版本
  pendingVersion?: string | null
  pendingAt?: string | null
  lastCheckedAt?: string | null
  lastCheckResult?: string | null
  foundVersion?: string | null
  downloadResult?: string | null
  failStage?: UpdateFailStage | null
  failReason?: string | null
  history?: string[]
}

const STAGES = new Set<UpdateFailStage>(['check', 'download', 'verify', 'install'])

// 环境变量里的测试更新源：只收 http(s) 地址（没有用户名密码）；其他一律当没设
export function parseFeedOverride(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const value = raw.trim()
  if (!value || value.length > 1024 || /[\s"'<>\\]/.test(value)) return null
  try {
    const url = new URL(value)
    if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.username || url.password || !url.hostname) return null
    return url.href
  } catch { return null }
}

// 出错信息 → 只留第一行、去掉太长的尾巴（写日志和诊断报告用；报告导出时还会再脱敏一遍）
export function shortReason(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error)
  const first = text.split(/\r?\n/).find(line => line.trim()) ?? ''
  return first.trim().slice(0, 300)
}

// 下载阶段出的错：校验不通过单独算一类
export function downloadFailStage(error: unknown): UpdateFailStage {
  const code = error && typeof error === 'object' ? String((error as { code?: unknown }).code ?? '') : ''
  const text = error instanceof Error ? error.message : String(error)
  return code === 'ERR_CHECKSUM_MISMATCH' || /checksum mismatch|sha512|signature|not signed|校验/i.test(text) ? 'verify' : 'download'
}

export class AutoUpdateService {
  private state: UpdateState
  private persisted: Persisted
  private readonly listeners = new Set<(state: UpdateState) => void>()
  private backendPromise: Promise<UpdaterBackend | null> | null = null
  private checking: Promise<UpdateState> | null = null
  private downloading: Promise<UpdateState> | null = null
  private busyTimer: unknown = null
  // 上次的安装没成功（启动时发现的）：这次运行不再自动检查，免得把失败提示盖掉
  private installFailedOnStart = false

  constructor(private readonly options: AutoUpdateOptions) {
    this.state = { ...initialUpdateState(options.currentVersion), feedOverridden: !!options.feedOverridden }
    this.persisted = this.load()
    this.state.lastCheckedAt = this.persisted.lastCheckedAt ?? null
    if (options.feedOverridden) options.log(`自动更新：更新源已被环境变量 ${FEED_ENV} 改到测试地址（只给测试用）`)
    this.reconcilePending()
  }

  get(): UpdateState {
    const busy = this.safeBusy()
    return { ...this.state, notes: [...this.state.notes], busy, skipped: this.isSkipped(this.state.version) }
  }

  subscribe(listener: (state: UpdateState) => void): () => void {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  // manual=false：启动时的自动检查。设置里关了、正在下载 / 已下载、上次安装失败的提示还在时，都不发请求。
  check(manual: boolean): Promise<UpdateState> {
    if (this.checking) {
      if (manual && !this.state.manual) this.set({ manual: true })
      return this.checking
    }
    if (this.state.phase === 'downloading' || this.state.phase === 'downloaded') return Promise.resolve(this.get())
    if (!manual) {
      if (!this.options.autoCheck()) { this.options.log('自动更新：设置里关了自动检查，这次不查'); return Promise.resolve(this.get()) }
      if (this.installFailedOnStart && this.state.phase === 'failed') return Promise.resolve(this.get())
    }
    this.checking = this.runCheck(manual).finally(() => { this.checking = null })
    return this.checking
  }

  // "立即更新"：只在"发现新版本"时有效；重复点不会重复下载
  download(): Promise<UpdateState> {
    if (this.downloading) return this.downloading
    if (this.state.phase !== 'available') return Promise.resolve(this.get())
    this.downloading = this.runDownload().finally(() => { this.downloading = null })
    return this.downloading
  }

  // 失败后的"重试"：连不上 → 再查一次；下载 / 校验 / 安装失败 → 再查一次，有新版本就接着下载
  async retry(): Promise<UpdateState> {
    if (this.state.phase !== 'failed') return this.get()
    const stage = this.state.failStage
    this.installFailedOnStart = false
    const checked = await this.check(true)
    if (stage !== 'check' && checked.phase === 'available') return this.download()
    return checked
  }

  // "以后再说" / 关掉失败提示：本次运行不再提示（下次启动照常）
  later(): UpdateState {
    if (this.state.phase === 'available' || this.state.phase === 'failed') this.set({ dismissed: true })
    return this.get()
  }

  // "跳过这个版本"：记进设置；以后自动检查发现的还是这个版本就不提示（手动检查照常显示）
  skip(): UpdateState {
    const version = this.state.version
    if (this.state.phase !== 'available' || !version) return this.get()
    this.options.setIgnoredVersion(version)
    this.options.log(`自动更新：跳过版本 ${version}`)
    this.note(`跳过版本 ${version}`)
    this.set({ dismissed: true })
    return this.get()
  }

  // "重启安装"。出片进行中一律拒绝（不退出、不安装）；其他时候交给后端退出并安装。
  install(): { ok: boolean; reason?: 'not_downloaded' | 'busy' | 'failed' } {
    if (this.state.phase !== 'downloaded') return { ok: false, reason: 'not_downloaded' }
    if (this.safeBusy()) {
      this.options.log('自动更新：正在出片，没有执行"重启安装"')
      this.emit()
      return { ok: false, reason: 'busy' }
    }
    const version = this.state.version
    this.options.log(`自动更新：用户点了"重启安装"，退出并安装 ${version ?? ''}`)
    this.note(`重启安装 ${version ?? ''}`)
    try {
      const backend = this.loadedBackend
      if (!backend) throw new Error('更新程序没有准备好')
      backend.quitAndInstall()
      return { ok: true }
    } catch (error) {
      this.fail('install', error)
      return { ok: false, reason: 'failed' }
    }
  }

  // 手动下载地址：只有两个固定的，且都要过白名单
  linkFor(which: unknown): string | null {
    const url = which === 'quark' ? QUARK_DOWNLOAD_URL : which === 'github' ? GITHUB_RELEASES_URL : null
    return url && isAllowedUpdateLink(url) ? url : null
  }

  diagnostics(): UpdateDiagnostics {
    return {
      currentVersion: this.options.currentVersion, feedOverridden: !!this.options.feedOverridden, autoCheck: this.safe(() => this.options.autoCheck(), true),
      lastCheckedAt: this.persisted.lastCheckedAt ?? null, lastCheckResult: this.persisted.lastCheckResult ?? null,
      foundVersion: this.persisted.foundVersion ?? null, downloadResult: this.persisted.downloadResult ?? null,
      failStage: this.persisted.failStage ?? null, failReason: this.persisted.failReason ?? null, history: [...(this.persisted.history ?? [])]
    }
  }

  dispose(): void { this.stopBusyWatch() }

  // ---- 内部 ----

  private loadedBackend: UpdaterBackend | null = null

  private async backend(): Promise<UpdaterBackend | null> {
    if (!this.backendPromise) {
      this.backendPromise = Promise.resolve().then(() => this.options.backend()).then(backend => { this.loadedBackend = backend; return backend })
      // 装载失败下次再试
      this.backendPromise.catch(() => { this.backendPromise = null })
    }
    return this.backendPromise
  }

  private async runCheck(manual: boolean): Promise<UpdateState> {
    const at = this.now().toISOString()
    this.set({ phase: 'checking', manual, failStage: null, dismissed: manual ? false : this.state.dismissed })
    let result: BackendCheckResult
    try {
      const backend = await this.backend()
      if (!backend) {
        this.options.log('自动更新：这个版本不支持（开发模式或没有更新配置），没有检查')
        this.set({ phase: 'unsupported', lastCheckedAt: at })
        return this.get()
      }
      result = await this.withTimeout(backend.check(), this.options.checkTimeoutMs ?? CHECK_TIMEOUT_MS)
    } catch (error) {
      const reason = shortReason(error)
      this.options.log(`自动更新：${manual ? '手动' : '自动'}检查失败（${reason}）`)
      this.remember({ lastCheckedAt: at, lastCheckResult: '检查失败', failStage: 'check', failReason: reason })
      this.note(`检查失败：${reason}`)
      // 自动检查连不上：什么都不显示；手动检查：在"关于"页里说，并给手动下载地址
      this.set(manual ? { phase: 'failed', failStage: 'check', lastCheckedAt: at } : { phase: 'idle', lastCheckedAt: at })
      return this.get()
    }
    const latest = parseSemver(result.version)
    const current = parseSemver(this.options.currentVersion)
    const newer = result.available && !!latest && !!current && compareSemver(latest, current) > 0
    if (!newer) {
      this.options.log(`自动更新：${manual ? '手动' : '自动'}检查，已是最新（当前 ${this.options.currentVersion}，线上 ${result.version || '未知'}）`)
      this.remember({ lastCheckedAt: at, lastCheckResult: '已是最新', foundVersion: null, failStage: null, failReason: null })
      this.note(`检查：已是最新（线上 ${result.version || '未知'}）`)
      this.set({ phase: 'latest', version: null, notes: [], lastCheckedAt: at })
      return this.get()
    }
    const version = versionText(latest)
    const skipped = this.isSkipped(version)
    this.options.log(`自动更新：${manual ? '手动' : '自动'}检查，发现新版本 ${version}${skipped ? '（用户已选择跳过这个版本）' : ''}`)
    this.remember({ lastCheckedAt: at, lastCheckResult: `发现新版本 ${version}`, foundVersion: version, failStage: null, failReason: null })
    this.note(`检查：发现新版本 ${version}${skipped ? '（已跳过）' : ''}`)
    this.set({ phase: 'available', version, notes: cleanReleaseNotes(result.notes), percent: 0, transferredBytes: 0, totalBytes: 0, lastCheckedAt: at })
    return this.get()
  }

  private async runDownload(): Promise<UpdateState> {
    const version = this.state.version
    this.options.log(`自动更新：开始下载 ${version ?? ''}`)
    this.note(`开始下载 ${version ?? ''}`)
    this.set({ phase: 'downloading', percent: 0, transferredBytes: 0, totalBytes: 0, dismissed: false, failStage: null })
    try {
      const backend = await this.backend()
      if (!backend) throw new Error('更新程序没有准备好')
      await backend.download(progress => {
        if (this.state.phase !== 'downloading') return
        const percent = Math.max(0, Math.min(100, Math.floor(Number(progress.percent) || 0)))
        this.set({ percent, transferredBytes: Math.max(0, Number(progress.transferred) || 0), totalBytes: Math.max(0, Number(progress.total) || 0) })
      })
    } catch (error) {
      this.fail(downloadFailStage(error), error)
      return this.get()
    }
    this.options.log(`自动更新：${version ?? ''} 已下载并通过校验；关闭软件时自动安装，或点"重启安装"`)
    this.remember({ downloadResult: `已下载 ${version ?? ''}`, pendingVersion: version, pendingAt: this.now().toISOString(), failStage: null, failReason: null })
    this.note(`已下载 ${version ?? ''}`)
    this.set({ phase: 'downloaded', percent: 100, installOnQuit: true })
    this.startBusyWatch()
    return this.get()
  }

  private fail(stage: UpdateFailStage, error: unknown): void {
    const reason = shortReason(error)
    const label = { check: '检查失败', download: '下载失败', verify: '校验失败', install: '安装失败' }[stage]
    this.options.log(`自动更新：${label}（${reason}）`)
    this.remember({ failStage: stage, failReason: reason, ...(stage === 'install' ? {} : { downloadResult: label }), pendingVersion: null, pendingAt: null })
    this.note(`${label}：${reason}`)
    this.stopBusyWatch()
    this.set({ phase: 'failed', failStage: stage, dismissed: false, installOnQuit: false })
  }

  // 启动时：上次下载好的版本装上了没有
  private reconcilePending(): void {
    const pending = parseSemver(this.persisted.pendingVersion)
    if (!pending) return
    const current = parseSemver(this.options.currentVersion)
    const version = versionText(pending)
    if (current && compareSemver(current, pending) >= 0) {
      this.options.log(`自动更新：已更新到 ${this.options.currentVersion}`)
      this.remember({ pendingVersion: null, pendingAt: null, downloadResult: `已安装 ${version}`, failStage: null, failReason: null })
      this.note(`已更新到 ${this.options.currentVersion}`)
      return
    }
    const reason = `上次下载的 ${version} 没有装上（现在仍是 ${this.options.currentVersion}）`
    this.options.log(`自动更新：安装失败（${reason}）`)
    this.remember({ pendingVersion: null, pendingAt: null, failStage: 'install', failReason: reason })
    this.note(`安装失败：${reason}`)
    this.installFailedOnStart = true
    this.state = { ...this.state, phase: 'failed', failStage: 'install', version }
  }

  private isSkipped(version: string | null): boolean {
    const ignored = parseSemver(this.safe(() => this.options.ignoredVersion(), null))
    const found = parseSemver(version)
    return !!ignored && !!found && compareSemver(ignored, found) === 0
  }

  // 已下载之后盯着"是不是在出片"：变了就推一次状态，让"重启安装"按钮跟着置灰 / 恢复
  private startBusyWatch(): void {
    if (this.busyTimer !== null) return
    let last = this.safeBusy()
    const tick = (): void => {
      const now = this.safeBusy()
      if (now !== last) { last = now; this.emit() }
    }
    const timer = (this.options.setInterval ?? ((fn, ms) => { const t = setInterval(fn, ms); t.unref?.(); return t }))(tick, BUSY_POLL_MS)
    this.busyTimer = timer
  }

  private stopBusyWatch(): void {
    if (this.busyTimer === null) return
    ;(this.options.clearInterval ?? (timer => clearInterval(timer as ReturnType<typeof setInterval>)))(this.busyTimer)
    this.busyTimer = null
  }

  private safeBusy(): boolean { return this.safe(() => this.options.busy(), false) }

  private safe<T>(fn: () => T, fallback: T): T { try { return fn() } catch { return fallback } }

  private now(): Date { return (this.options.now ?? (() => new Date()))() }

  private withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`检查更新超过 ${Math.round(ms / 1000)} 秒没有回应`)), ms)
      promise.then(value => { clearTimeout(timer); resolve(value) }, error => { clearTimeout(timer); reject(error instanceof Error ? error : new Error(String(error))) })
    })
  }

  private set(patch: Partial<UpdateState>): void {
    this.state = { ...this.state, ...patch }
    this.emit()
  }

  private emit(): void {
    const snapshot = this.get()
    for (const listener of this.listeners) { try { listener(snapshot) } catch { /* 推送失败不影响更新流程 */ } }
  }

  private note(line: string): void {
    const history = [...(this.persisted.history ?? []), `${this.now().toISOString()} ${line}`].slice(-HISTORY_MAX)
    this.remember({ history })
  }

  private remember(patch: Persisted): void {
    this.persisted = { ...this.persisted, ...patch }
    const file = this.options.stateFile
    if (!file) return
    try {
      mkdirSync(dirname(file), { recursive: true })
      const temp = `${file}.tmp`
      writeFileSync(temp, JSON.stringify(this.persisted, null, 2), 'utf8')
      renameSync(temp, file)
    } catch { /* 记不下来不影响更新 */ }
  }

  private load(): Persisted {
    const file = this.options.stateFile
    if (!file || !existsSync(file)) return {}
    try {
      const raw: unknown = JSON.parse(readFileSync(file, 'utf8'))
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
      const r = raw as Record<string, unknown>
      const str = (value: unknown): string | null => typeof value === 'string' && value.length <= 400 ? value : null
      return {
        pendingVersion: str(r.pendingVersion), pendingAt: str(r.pendingAt), lastCheckedAt: str(r.lastCheckedAt), lastCheckResult: str(r.lastCheckResult),
        foundVersion: str(r.foundVersion), downloadResult: str(r.downloadResult),
        failStage: STAGES.has(r.failStage as UpdateFailStage) ? r.failStage as UpdateFailStage : null, failReason: str(r.failReason),
        history: Array.isArray(r.history) ? r.history.filter((line): line is string => typeof line === 'string' && line.length <= 500).slice(-HISTORY_MAX) : []
      }
    } catch { return {} }
  }
}
