// 引擎 stdout 事件（《新文章输入约定 v5.3》第 2 节）。只声明 APP 用到的字段；
// 其余字段和不认识的事件一律忽略，不报错。

export interface BaseEvent {
  schema_version: number
  seq: number
  time: string
  event: string
  step: string | null
  message: string
}

export interface InputProblem {
  kind: string
  detail: string
  hint: string
  line?: number
}

export interface InputResolvedEvent extends BaseEvent {
  event: 'input_resolved'
  article: string
  work_dir: string
  source_type: 'feishu' | 'markdown'
  title: string
  reused: boolean
  changed: boolean
  image_count: number
  content_type: 'theory' | 'screenshot' | null
  warnings: InputProblem[]
}

export interface InputProblemsEvent extends BaseEvent {
  event: 'input_problems'
  problems: InputProblem[]
}

export interface StepStartEvent extends BaseEvent { event: 'step_start'; step: string; name?: string }
export interface StepSkipEvent extends BaseEvent { event: 'step_skip'; step: string; name?: string }

export interface StepProgressEvent extends BaseEvent {
  event: 'step_progress'
  step: string
  done: number
  total: number
  unit: string
  // 引擎网络重试时在同一种事件里带上这一项（ST3-P26；约定 v5.2 正文没有列出，按"可选字段"处理）
  retry?: { service: string; what?: string; attempt: number; wait_sec: number; reason?: string }
}

export interface StepDoneEvent extends BaseEvent { event: 'step_done'; step: string; name?: string; estimated_cny?: number; content_type?: string }

// service：约定 v5.3 第 2.8 节，失败来自哪家外部服务（null = 不来自外部服务；旧引擎没有这个字段）
export interface StepFailEvent extends BaseEvent { event: 'step_fail'; step: string; error_kind: string; name?: string; service?: string | null }

export interface EstimateRange { low_cny: number; high_cny: number }

export interface EstimateEvent extends BaseEvent {
  event: 'estimate'
  low_cny: number
  high_cny: number
  by_service: Record<string, EstimateRange & { service_name: string; price_estimated: boolean }>
  saved_by_cache_cny: number
  basis: {
    chars?: number
    images?: number
    content_type?: 'theory' | 'screenshot' | null
    content_type_basis?: string
    illustrations?: [number, number]
    [key: string]: unknown
  }
}

export interface KeyCheckEvent extends BaseEvent {
  event: 'key_check'
  service: string
  service_name: string
  status: string
  env_vars: string[]
  missing_keys?: string[]
  balance?: { currency: string | null; total: string | number | null }[]
}

export interface RunDoneEvent extends BaseEvent {
  event: 'run_done'
  exit_code: number
  reason?: string
  article?: string
  error_kind?: string
  service?: string | null
  missing_keys?: string[]
  report_json?: string | null
  delivery_path?: string | null
  results?: Record<string, string>
  // APP 自己补的 run_done（进程被强制结束、没发出 run_done 时），不是引擎发的
  synthesized?: boolean
}

export type EngineEvent =
  | InputResolvedEvent | InputProblemsEvent | StepStartEvent | StepSkipEvent | StepProgressEvent
  | StepDoneEvent | StepFailEvent | EstimateEvent | KeyCheckEvent | RunDoneEvent

const KNOWN = new Set(['input_resolved', 'input_problems', 'step_start', 'step_skip', 'step_progress', 'step_done',
  'step_fail', 'estimate', 'key_check', 'run_done'])

// 检查结果 10-报告/检查结果.json（约定第 7 节）
export interface CheckReportItem {
  level: 'red' | 'notice'
  kind: string
  message: string
  time: string | null
  detail_file: string | null
}

export interface CheckReport {
  schema_version: number
  verdict: 'pass' | 'review' | 'fail'
  conclusion: string
  rendered: boolean
  items: CheckReportItem[]
  generated_at: string
  generated_by: string
  stale: boolean
  stale_detail: unknown
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

// 认得的事件返回原对象（多余字段保留但不使用），不认得的返回 null，由调用方忽略。
export function asKnownEvent(value: unknown): EngineEvent | null {
  if (!isObject(value) || typeof value.event !== 'string' || !KNOWN.has(value.event)) return null
  if (typeof value.seq !== 'number' || typeof value.message !== 'string') return null
  return value as unknown as EngineEvent
}
