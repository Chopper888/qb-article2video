// 任务列表：每篇文章文件夹读出来的事实 → 列表上的状态（任务书 APP2 3.3）。
// 读不到的字段为 null，界面显示"—"，不报错。

export type TaskState = 'running' | 'done' | 'done_red' | 'stopped' | 'failed'

export interface TaskFacts {
  article: string
  title: string | null
  source: '飞书' | 'Markdown' | null
  contentType: 'theory' | 'screenshot' | null
  // 进度账本里每一步的状态（done / imported / skipped / running / pending / failed …）
  steps: { step: string; status: string; message?: string }[] | null
  costCny: number | null
  reds: number | null
  verdict: 'pass' | 'review' | 'fail' | null
  updatedAt: string | null
  // APP 里这篇正有引擎进程在跑
  active: boolean
  // APP3：已完成且中间产物文件夹里除 cache/ 以外都清掉了（"清理中间文件"做过）
  cleaned?: boolean
}

export interface TaskRow extends TaskFacts {
  state: TaskState
  stateText: string
  stepDone: number | null
  stepTotal: number | null
}

const FINISHED = new Set(['done', 'imported', 'skipped'])

export function deriveTaskState(facts: TaskFacts): { state: TaskState; stateText: string } {
  if (facts.active) return { state: 'running', stateText: '出片中' }
  const steps = facts.steps ?? []
  const failed = steps.find(step => step.status === 'failed')
  if (failed) return { state: 'failed', stateText: '失败' }
  const allDone = steps.length > 0 && steps.every(step => FINISHED.has(step.status))
  if (allDone) {
    const cleaned = facts.cleaned ? ' · 已清理中间文件' : ''
    if ((facts.reds ?? 0) > 0 || facts.verdict === 'fail') return { state: 'done_red', stateText: `已完成 · ${facts.reds ?? 0} 处标红${cleaned}` }
    return { state: 'done', stateText: `已完成${cleaned}` }
  }
  // 没有账本：只导入过、还没开始（导入有问题的也在这里）
  if (!steps.length) return { state: 'stopped', stateText: '已导入 · 未开始' }
  // 跑了一半（含 APP 异常退出后留下的 running）：停下了，可以继续
  return { state: 'stopped', stateText: '已停止 · 可继续' }
}

export function toTaskRow(facts: TaskFacts): TaskRow {
  const { state, stateText } = deriveTaskState(facts)
  const steps = facts.steps
  return {
    ...facts, state, stateText,
    stepDone: steps ? steps.filter(step => FINISHED.has(step.status)).length : null,
    stepTotal: steps ? steps.length : null
  }
}

export const TASK_TONE: Record<TaskState, string> = { running: 'tone-warn', done: 'tone-ok', done_red: 'tone-danger', stopped: 'tone-muted', failed: 'tone-danger' }

export const TASK_FILTERS: { id: 'all' | TaskState | 'need'; label: string }[] = [
  { id: 'all', label: '全部' }, { id: 'running', label: '进行中' }, { id: 'done', label: '已完成' }, { id: 'need', label: '需查看' }, { id: 'stopped', label: '已停止' }
]

export function filterTasks<T extends { state: TaskState }>(tasks: T[], filter: string): T[] {
  if (filter === 'all') return tasks
  if (filter === 'need') return tasks.filter(task => task.state === 'done_red' || task.state === 'failed')
  if (filter === 'done') return tasks.filter(task => task.state === 'done' || task.state === 'done_red')
  return tasks.filter(task => task.state === filter)
}

// 成片库的一张卡片
export interface VideoItem {
  file: string
  name: string
  article: string | null
  title: string
  durationSec: number | null
  // 封面 data URL（main 截好缓存，读不到为 null）
  cover: string | null
  reds: number | null
  notices: number | null
  verdict: 'pass' | 'review' | 'fail' | null
  mtime: string
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—'
  const total = Math.round(seconds)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

// 磁盘占用（字节），统计时忽略 _a2v_tmp
export interface DiskUsageReal { completed: number; unfinished: number; cache: number; total: number }

// "清理已完成任务的中间文件"（APP3 起真正删除，移到回收站）：按文章列出要移走的东西
export interface CleanupArticle { article: string; title: string | null; bytes: number; items: { path: string; bytes: number }[] }
export interface CleanupPreview { articles: CleanupArticle[]; totalBytes: number }
export interface CleanupResult {
  ok: boolean
  // 已移到回收站的文章、条目数、字节数
  articles: string[]
  trashed: number
  bytes: number
  // 没做完时的原因（回收站不可用、某项移不动……）；其余没动
  error?: string
}
