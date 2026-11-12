import type { DemoImportScenario, DemoRunScenario, KeyCheckOutcome } from './adapter'
import type { ExternalLinkId } from './links'
import type { UpdateState } from './auto-update'
import type { KeyFieldId, KeyStatusMap } from './keys'
import type { ChapterList } from './chapters'
import type { CheckReport, EngineEvent } from './protocol'
import type { CleanupPreview, CleanupResult, DiskUsageReal, TaskRow, VideoItem } from './tasks'
import type { ThirdPartyComponent } from './licenses'

export type ThemeName = 'dark' | 'light'

// 上次检测：每项服务的状态和时间（余额不存盘）
export interface KeyCheckRecord { checkedAt: string; statuses: Record<string, string> }

export interface V1Prefs {
  onboarded: boolean
  workDir: string
  outputDir: string
  maxCostCny: number
  theme: ThemeName
  // R120M P1：出图（普通设置，非密钥），通过环境变量 A2V_IMAGE_BACKEND / A2V_MOSS_STYLE /
  // A2V_COMFYUI_URL / A2V_MOSS_FALLBACK_GPT 传给引擎。出图方式为 GPT 时后三项不起作用（界面置灰）。
  imageBackend: ImageBackend
  mossStyle: MossStyle
  comfyuiUrl: string
  mossFallbackGpt: boolean
  // R120M2 Q2：启动时自动检查更新（默认开）；"忽略这个版本"记住的版本号（同一版本不再提醒）
  autoCheckUpdate: boolean
  ignoredUpdateVersion: string | null
  lastKeyCheck: KeyCheckRecord | null
}

// R120M3：新增 moss_rh（RunningHub 上的 MOSS 应用，消耗 RH 币，不需要本地显卡）
export type ImageBackend = 'gpt' | 'moss_local' | 'moss_rh'
export const IMAGE_BACKENDS: { id: ImageBackend; name: string }[] = [
  { id: 'gpt', name: 'GPT（RunningHub）' }, { id: 'moss_local', name: 'MOSS 本地（ComfyUI）' }, { id: 'moss_rh', name: 'MOSS · RH 云端' }
]
// R121M：RH 同时出图数不再是设置项（引擎自动调节：起始 3，RunningHub 回并发 / 队列已满时降为 1）。
// 正式版 4 种风格（S4 扁平矢量为新增）。
export type MossStyle = 'S0' | 'S3' | 'S1' | 'S4'
export const MOSS_STYLES: { id: MossStyle; name: string }[] = [
  { id: 'S0', name: '手绘线稿' }, { id: 'S3', name: '科技霓虹' }, { id: 'S1', name: '日系水彩' }, { id: 'S4', name: '扁平矢量' }
]
export const DEFAULT_COMFYUI_URL = 'http://127.0.0.1:8188'

export interface EngineInfo {
  version: string | null
  root: string | null
  python: string | null
  // 运行环境：引擎目录、Python 是否找得到
  status: 'ready' | 'missing'
  problems: string[]
}

export interface AppInfo {
  appVersion: string
  platform: string
  isPackaged: boolean
  // 开发者开关（演示场景切换）只在开发模式或显式打开时提供
  devTools: boolean
  engine: EngineInfo
  // 本轮哪些操作是演示实现
  adapterKinds: { article: 'demo' | 'real'; keyCheck: 'demo' | 'real' }
}

export interface DemoState {
  importScenario: DemoImportScenario
  runScenario: DemoRunScenario
  delayMs: number
  // APP2：导入 / 估算 / 出片走真实引擎还是演示场景（普通用户固定 real）
  mode: 'real' | 'demo'
  // APP2：开发者开关"不付费试跑"——运行和续跑带 --no-paid。APP4：只在内存里，每次启动都是关的
  noPaid: boolean
}

// APP3：安装版首次启动准备运行环境（把 Runtime Pack 的 runtime 复制到短路径）。开发模式不需要（not_needed）
export type RuntimePrep =
  | { status: 'not_needed' }
  | { status: 'ready'; dir: string }
  | { status: 'copying'; percent: number; doneFiles: number; totalFiles: number }
  | { status: 'failed'; error: string }

export function runtimePrepBusy(prep: RuntimePrep | null): boolean {
  return !!prep && prep.status !== 'ready' && prep.status !== 'not_needed'
}

export type ExportResult = { ok: true; file: string; bytes: number; entries: string[] } | { ok: false; canceled?: boolean; error?: string }

// 引擎事件推给页面时带上 session id
export interface SessionEvent { sessionId: string; event: EngineEvent }
export interface SessionEnd { sessionId: string; exitCode: number | null }

export type DiskUsage = DiskUsageReal

export interface V1Api {
  getAppInfo(): Promise<AppInfo>
  getPrefs(): Promise<V1Prefs>
  updatePrefs(patch: Partial<V1Prefs>): Promise<V1Prefs>
  getKeyStatus(): Promise<KeyStatusMap>
  setKey(id: KeyFieldId, value: string): Promise<KeyStatusMap>
  checkKeys(): Promise<KeyCheckOutcome>
  openExternal(link: ExternalLinkId): Promise<void>
  openFolder(target: 'work' | 'output' | 'logs'): Promise<void>
  chooseFolder(target: 'work' | 'output'): Promise<V1Prefs>
  importArticle(input: string): Promise<string>
  estimate(article: string): Promise<string>
  run(article: string, maxCostCny: number): Promise<string>
  resume(article: string, maxCostCny: number): Promise<string>
  stop(sessionId: string): Promise<void>
  readReport(article: string): Promise<CheckReport | null>
  onSessionEvent(listener: (event: SessionEvent) => void): () => void
  onSessionEnd(listener: (end: SessionEnd) => void): () => void
  getDemoState(): Promise<DemoState | null>
  setDemoState(patch: Partial<DemoState>): Promise<DemoState | null>
  onDevScene(listener: (scene: string) => void): () => void
  // APP2
  chooseImportFolder(): Promise<string | null>
  pathForFile(file: File): string
  readChapters(article: string): Promise<ChapterList | null>
  openArticleSource(input: string): Promise<void>
  listTasks(): Promise<TaskRow[]>
  listVideos(): Promise<VideoItem[]>
  diskUsage(): Promise<DiskUsageReal>
  cleanupPreview(): Promise<CleanupPreview>
  // R120M P2：导出完整日志（zip，默认存桌面，已脱敏）
  exportDiagnostics(): Promise<ExportResult>
  // R120M P2：导出诊断报告（.md，默认存桌面，已脱敏）
  exportDiagnosticsReport(): Promise<ExportResult>
  // R121M5：APP 内自动更新。状态由 Main 维护并推送；页面只发"检查 / 立即更新 / 重启安装 / 以后再说 / 跳过 / 重试"。
  // manual=false 是启动时的自动检查（设置关了不请求）；手动下载链接只能选 github / quark，地址由 Main 决定
  checkUpdate(manual: boolean): Promise<UpdateState>
  openUpdateLink(which: 'github' | 'quark'): Promise<void>
  getUpdateState(): Promise<UpdateState>
  downloadUpdate(): Promise<UpdateState>
  // 出片进行中 Main 会拒绝（ok=false, reason='busy'），不会退出
  installUpdate(): Promise<{ ok: boolean; reason?: 'not_downloaded' | 'busy' | 'failed' }>
  laterUpdate(): Promise<UpdateState>
  skipUpdate(): Promise<UpdateState>
  retryUpdate(): Promise<UpdateState>
  onUpdateState(listener: (state: UpdateState) => void): () => void
  mediaUrl(file: string): string
  // APP3
  getRuntimePrep(): Promise<RuntimePrep>
  retryRuntimePrep(): Promise<RuntimePrep>
  onRuntimePrep(listener: (state: RuntimePrep) => void): () => void
  cleanupRun(articles: string[]): Promise<CleanupResult>
  listLicenses(): Promise<ThirdPartyComponent[]>
  openLicense(id: string): Promise<void>
  // APP4
  // 选择本地文章：'any' = 文件夹或 .zip / .md 都能选（Mac）；Windows 上一个选择框不能同时选文件和文件夹，分成 'folder' / 'file'
  chooseImportSource(kind: ImportSourceKind): Promise<string | null>
  // 一次拖进多个文件：Main 复制进 _a2v_import/ 的新文件夹再导入；input 是那个文件夹
  importDropped(paths: string[]): Promise<{ sessionId: string; input: string }>
  articleInfo(article: string): Promise<ArticleInfo | null>
  onPrefsChanged(listener: (prefs: V1Prefs) => void): () => void
  reloadWindow(): Promise<void>
  openDevTools(): Promise<void>
  // 1.0.1（R101 J）：删除任务。先问要移到回收站的是哪个文件夹（确认框里显示），确认后再删
  deleteTaskCheck(article: string): Promise<DeleteTaskOutcome>
  deleteTask(article: string): Promise<DeleteTaskOutcome>
}

export type DeleteTaskOutcome = { ok: true; article: string; path: string } | { ok: false; article: string; error: string }

export type ImportSourceKind = 'any' | 'folder' | 'file'

// APP4：一篇文章的标题（已去转义）和类型（账本 → S0 结果）
export interface ArticleInfo { article: string; title: string | null; contentType: 'theory' | 'screenshot' | null; source: '飞书' | 'Markdown' | null }
