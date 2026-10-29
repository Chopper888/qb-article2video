import type { CheckReport, EngineEvent, KeyCheckEvent } from './protocol'

// 引擎适配层（任务书第五节）。界面只认这一组操作和约定 v5.2 的事件；
// 本轮：checkKeys 是真实实现，其余是演示实现；APP2 只换实现，界面不改。

export interface ImportRequest { input: string; workRoot: string }
export interface ArticleRequest { article: string; workRoot: string }
export interface RunRequest extends ArticleRequest { maxCostCny: number }

// 每个操作是一次引擎进程（session）：立即拿到 sessionId（停止要用），事件按顺序推给 onEvent，
// done 在进程结束时给出退出码（没有 run_done 时为 null）。
export interface EngineSession { sessionId: string; done: Promise<number | null> }
export type EventSink = (event: EngineEvent) => void

export interface ArticleEngineAdapter {
  readonly kind: 'demo' | 'real'
  importArticle(request: ImportRequest, onEvent: EventSink): EngineSession
  estimate(request: ArticleRequest, onEvent: EventSink): EngineSession
  run(request: RunRequest, onEvent: EventSink): EngineSession
  // 续跑 / 补图：run --article，不带 --from（约定第 5 节）
  resume(request: RunRequest, onEvent: EventSink): EngineSession
  stop(sessionId: string): Promise<void>
  readReport(request: ArticleRequest): Promise<CheckReport | null>
}

export interface KeyCheckAdapter {
  readonly kind: 'demo' | 'real'
  checkKeys(services?: string[]): Promise<KeyCheckOutcome>
}

export interface KeyCheckOutcome {
  ok: boolean
  checkedAt: string
  results: KeyCheckEvent[]
  // 检测没做成（引擎起不来、超时、退出码不是 0）时给用户看的一句话
  error: string | null
}

// 演示场景（开发者开关里切换）。每个场景对应第三节列出的一种状态。
export const DEMO_IMPORT_SCENARIOS = ['import_problems', 'import_pass', 'import_over_limit'] as const
export const DEMO_RUN_SCENARIOS = [
  'run_theory', 'run_screenshot', 'run_render', 'network_retry', 'balance_insufficient', 'key_invalid', 'cost_limit',
  'missing_images', 'stopped', 'done_pass', 'done_red'
] as const
export type DemoImportScenario = typeof DEMO_IMPORT_SCENARIOS[number]
export type DemoRunScenario = typeof DEMO_RUN_SCENARIOS[number]
export type DemoScenario = DemoImportScenario | DemoRunScenario
