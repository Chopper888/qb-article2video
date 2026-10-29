import type { DemoImportScenario, DemoRunScenario, KeyCheckOutcome } from './adapter'
import type { ExternalLinkId } from './links'
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
  lastKeyCheck: KeyCheckRecord | null
}

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
  exportDiagnostics(): Promise<ExportResult>
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
