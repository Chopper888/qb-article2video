import { randomUUID } from 'node:crypto'
import { createEngineAdapters } from '../engine/create-engine-adapters'
import type { AdapterFactory, EngineAdapters } from '../engine/create-engine-adapters'
import { WindowsProcessInspector } from '../engine/process-inspector'
import { TaskkillProcessTreeTerminator } from '../engine/process-tree-terminator'
import { createRealTaskRecord, LEGACY_RUNNING_MESSAGE, REAL_NOT_CONNECTED } from '../shared/real-task'
import { isCreateTaskInput, isSettingsPatch } from '../shared/validation'
import type {
  AppSettings,
  AppState,
  CreateTaskInput,
  EngineEvent,
  MockOutput,
  MockTask,
  RealTask,
  Task,
  StateStorage,
  TaskLog
} from '../shared/types'
import { LegacyProcessCoordinator, type LegacyInterruptionReason } from './legacy-process-coordinator'
import { appendBoundedDiagnosticText, appendBoundedStderr } from '../shared/stderr-tail'

const TERMINAL_STATUSES = new Set(['completed', 'quality_issue', 'failed', 'stopped', 'interrupted'])
const DIAGNOSTICS_SAVE_DEBOUNCE_MS = 250
const INTERRUPTION_MESSAGES: Record<LegacyInterruptionReason, string> = {
  missing_pid: 'APP 上次异常结束，缺少可核实运行进程，未确认任务正常完成。',
  process_missing: 'APP 上次异常结束后，原运行进程已经不存在，未确认任务正常完成。',
  process_mismatch: '原 PID 当前属于其它或不匹配进程，APP 未执行终止。',
  process_identity_unknown: '无法确认原进程身份，APP 为避免误杀未执行终止。'
}

export class TaskManager {
  private readonly engines: EngineAdapters
  private readonly listeners = new Set<(state: AppState) => void>()
  private saveChain: Promise<void> = Promise.resolve()
  private diagnosticsSaveTimer: ReturnType<typeof setTimeout> | null = null
  private recoveryState: 'pending' | 'done' = 'pending'

  private constructor(
    private readonly storage: StateStorage,
    private state: AppState,
    tickMs: number,
    factory: AdapterFactory,
    private readonly legacy: LegacyProcessCoordinator,
    private readonly now: () => string
  ) {
    this.engines = factory((event) => this.applyEngineEvent(event), tickMs)
  }

  static async create(
    storage: StateStorage,
    tickMs = 550,
    factory: AdapterFactory = createEngineAdapters,
    legacy = new LegacyProcessCoordinator(new WindowsProcessInspector(), new TaskkillProcessTreeTerminator()),
    now: () => string = () => new Date().toISOString()
  ): Promise<TaskManager> {
    const state = await storage.load()
    normalizePersistedDiagnostics(state)
    const manager = new TaskManager(storage, state, tickMs, factory, legacy, now)
    await manager.recoverPersistedTasks()
    return manager
  }

  snapshot(): AppState {
    return structuredClone(this.state)
  }

  subscribe(listener: (state: AppState) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async createTask(input: CreateTaskInput): Promise<MockTask> {
    if (!isCreateTaskInput(input)) throw new Error('无效的 Mock 任务输入（标题最多 200 字）')
    const now = new Date().toISOString()
    const task: MockTask = {
      id: randomUUID(),
      title: input.title.trim() || '未命名模拟任务',
      source: 'mock',
      scenario: input.scenario,
      status: 'pending',
      progress: 0,
      currentStep: null,
      createdAt: now,
      updatedAt: now,
      startedAt: null,
      finishedAt: null,
      error: null,
      logs: [createLog('info', '已创建 Mock 任务，尚未开始')]
    }
    this.state.tasks.unshift(task)
    await this.persistAndNotify()
    return structuredClone(task)
  }

  async startTask(taskId: string): Promise<void> {
    if (this.recoveryState !== 'done') throw new Error('APP 正在核实上次运行状态，请稍后再试')
    const task = this.requireTask(taskId)
    if (task.status === 'running' || task.status === 'queued') return
    if (task.source === 'real') {
      if (task.sample && task.sample !== 'sample-v4.1') throw new Error('历史 v3.2 联调任务仅保留查看，不支持从当前 v4.1 APP 启动')
      if (this.legacy.getVerifiedTaskId()) throw new Error('检测到上次任务仍在后台运行；请先停止上次仍在后台运行的任务')
      if (this.engines.mock.getRunningTaskId()) throw new Error('已有 Mock 任务运行；Real 准备层不并发')
      if (task.sample) {
        if (task.status !== 'pending' || this.state.tasks.some(item => item.source === 'real' && item.sample && item.startedAt && item.preparation?.status !== 'failed')) throw new Error('本轮真实联调已开始过；不支持第二次 run / Retry / Resume')
        task.status = 'running'
        task.startedAt = new Date().toISOString()
        task.updatedAt = task.startedAt
        // Persist the reservation before any process or runtime preparation can begin.
        await this.persistAndNotify()
      }
      this.engines.real.start(structuredClone(task))
    } else {
      if (this.engines.real.getRunningTaskId() || this.state.tasks.some(item => item.source === 'real' && item.status === 'running')) throw new Error('已有 Real 观察任务；不并发')
      this.engines.mock.start(structuredClone(task))
    }
    await this.saveChain
  }

  async stopTask(taskId: string): Promise<void> {
    const task = this.requireTask(taskId)
    if (task.source === 'real') {
      if (task.status !== 'running') throw new Error('Real 任务当前不在运行，不能停止')
      if (this.engines.real.getRunningTaskId() === taskId) {
        await this.engines.real.stop(taskId)
        await this.saveChain
        return
      }
      if (this.legacy.isVerified(taskId)) {
        const result = await this.legacy.stop(taskId)
        if (result.outcome === 'stopped') this.markLegacyStopped(task)
        else this.markInterrupted(task, result.reason)
        await this.persistAndNotify()
        return
      }
      throw new Error('Real 任务当前不在运行，不能停止')
    }
    await this.engines.mock.stop(taskId)
    await this.saveChain
  }

  async retryTask(taskId: string): Promise<void> {
    const task = this.requireTask(taskId)
    if (task.source === 'real') throw new Error(REAL_NOT_CONNECTED)
    if (task.status === 'running' || task.status === 'queued') throw new Error('运行或等待中的任务不能重试')
    task.status = 'pending'
    task.progress = 0
    task.currentStep = null
    task.finishedAt = null
    task.startedAt = null
    task.error = null
    task.updatedAt = new Date().toISOString()
    task.logs = [...task.logs, createLog('info', '已重新开始 Mock 任务')].slice(-200)
    this.engines.mock.start(structuredClone(task))
    await this.saveChain
  }

  async updateSettings(patch: Partial<AppSettings>): Promise<AppSettings> {
    if (!isSettingsPatch(patch)) throw new Error('无效的 APP 设置；不接受凭据或未知字段')
    this.state.settings = {
      ...this.state.settings,
      ...patch,
      directories: patch.directories
        ? { ...this.state.settings.directories, ...patch.directories }
        : this.state.settings.directories
    }
    await this.persistAndNotify()
    return structuredClone(this.state.settings)
  }

  getRunningTaskId(): string | null {
    return this.engines.mock.getRunningTaskId() ?? this.engines.real.getRunningTaskId() ?? this.legacy.getVerifiedTaskId()
  }

  getActiveRealTaskId(): string | null {
    const taskId = this.engines.real.getRunningTaskId() ?? this.legacy.getVerifiedTaskId()
    const task = taskId ? this.state.tasks.find(item => item.id === taskId) : null
    return task?.source === 'real' && task.status === 'running' ? taskId : null
  }

  async close(): Promise<void> {
    this.engines.mock.dispose()
    this.engines.real.dispose()
    await this.flushPendingDiagnostics()
  }

  // Internal controlled preparation entry only. Not exposed through IPC/preload/UI.
  async createRealTask(title: string): Promise<RealTask> {
    if (typeof title !== 'string' || title.length > 200) throw new Error('无效的 Real 任务标题')
    const task = createRealTaskRecord(randomUUID(), title.trim() || 'Real 接入准备', new Date().toISOString())
    this.state.tasks.unshift(task)
    await this.persistAndNotify()
    return structuredClone(task)
  }

  async createSampleTask(input: unknown): Promise<RealTask> {
    if (typeof input !== 'object' || input === null || Array.isArray(input) || Object.keys(input).length !== 1 || !('sample' in input) || input.sample !== 'sample-v4.1') throw new Error('仅允许示例篇 v4.1 开发联调样本')
    const task = createRealTaskRecord(randomUUID(), '真实联调：示例篇', new Date().toISOString())
    task.sample = 'sample-v4.1'
    this.state.tasks.unshift(task)
    await this.persistAndNotify()
    return structuredClone(task)
  }

  private requireTask(taskId: string): Task {
    const task = this.state.tasks.find((candidate) => candidate.id === taskId)
    if (!task) throw new Error(`任务不存在：${taskId}`)
    return task
  }

  private async recoverPersistedTasks(): Promise<void> {
    let changed = false
    for (const task of this.state.tasks) {
      if (task.source === 'real' && task.status === 'running') {
        const result = await this.legacy.recover(task)
        if (result.outcome === 'interrupted') {
          this.markInterrupted(task, result.reason)
          changed = true
        } else task.error = LEGACY_RUNNING_MESSAGE
        continue
      }
      if (task.source === 'mock' && (task.status === 'running' || task.status === 'queued')) {
        const observedAt = this.now()
        task.status = 'stopped'
        task.currentStep = null
        task.finishedAt = observedAt
        task.updatedAt = observedAt
        task.logs = [...task.logs, createLog('warning', 'APP 重启后，未完成的 Mock 任务恢复为已停止')].slice(-200)
        changed = true
      }
    }
    if (changed) await this.storage.save(this.snapshot())
    this.recoveryState = 'done'
  }

  private markInterrupted(task: RealTask, reason: LegacyInterruptionReason): void {
    const observedAt = this.now()
    const message = INTERRUPTION_MESSAGES[reason]
    task.status = 'interrupted'
    task.interruptionReason = reason
    task.currentStep = null
    task.error = message
    task.finishedAt = observedAt
    task.updatedAt = observedAt
    task.logs = [...task.logs, createLog('warning', message)].slice(-200)
  }

  private markLegacyStopped(task: RealTask): void {
    const observedAt = this.now()
    task.status = 'stopped'
    delete task.interruptionReason
    task.currentStep = null
    task.error = null
    task.finishedAt = observedAt
    task.updatedAt = observedAt
    if (task.preparation?.status === 'preparing') task.preparation.status = 'stopped'
    task.logs = [...task.logs, createLog('warning', '用户已停止上次仍在后台运行的真实任务')].slice(-200)
  }

  private applyEngineEvent(event: EngineEvent): void {
    const task = this.state.tasks.find((candidate) => candidate.id === event.taskId)
    if (!task) return
    const now = new Date().toISOString()
    if (task.source === 'real' && event.preparation) task.preparation = structuredClone(event.preparation)
    if (task.source === 'real' && event.real) {
      const previousCount = task.real.events.length
      task.real = structuredClone(event.real)
      for (const raw of task.real.events.slice(previousCount)) {
        task.logs.push(createLog(raw.event === 'step_fail' ? 'error' : 'info', `[${String(raw.event)}] ${String(raw.message)}`))
      }
      task.logs = task.logs.slice(-200)
    }
    if (event.status) task.status = event.status
    if (event.progress !== undefined) task.progress = event.progress
    if (event.currentStep !== undefined) task.currentStep = event.currentStep
    if (event.error !== undefined) {
      task.error = event.error
      if (event.error) this.state.lastError = event.error
    }
    if (event.log) task.logs = [...task.logs, createLog(event.log.level, event.log.message)].slice(-200)
    if (event.status === 'running' && !task.startedAt) task.startedAt = now
    if (event.status && TERMINAL_STATUSES.has(event.status)) task.finishedAt = now
    task.updatedAt = now

    if (event.status === 'completed' || event.status === 'quality_issue') {
      this.state.outputs = this.state.outputs.filter((output) => output.taskId !== task.id)
      if (task.source === 'mock') this.state.outputs.unshift(createOutput(task, now))
      else if (task.real.deliveryPath) this.state.outputs.unshift({
        id: randomUUID(), taskId: task.id, title: task.title, source: 'real',
        status: event.status, createdAt: now, deliveryPath: task.real.deliveryPath
      })
    } else if (task.source === 'real' && event.status === 'failed') {
      this.state.outputs = this.state.outputs.filter((output) => output.taskId !== task.id)
    }
    if (event.diagnosticsOnly === true && !(event.status && TERMINAL_STATUSES.has(event.status))) {
      this.scheduleDiagnosticsPersist()
      return
    }
    this.cancelPendingDiagnostics()
    void this.persistAndNotify().catch(() => undefined)
  }

  private scheduleDiagnosticsPersist(): void {
    if (this.diagnosticsSaveTimer !== null) return
    this.diagnosticsSaveTimer = setTimeout(() => {
      this.diagnosticsSaveTimer = null
      void this.persistAndNotify().catch(() => undefined)
    }, DIAGNOSTICS_SAVE_DEBOUNCE_MS)
  }

  private cancelPendingDiagnostics(): void {
    if (this.diagnosticsSaveTimer === null) return
    clearTimeout(this.diagnosticsSaveTimer)
    this.diagnosticsSaveTimer = null
  }

  private async flushPendingDiagnostics(): Promise<void> {
    if (this.diagnosticsSaveTimer !== null) {
      this.cancelPendingDiagnostics()
      await this.persistAndNotify()
      return
    }
    await this.saveChain
  }

  private async persistAndNotify(): Promise<void> {
    const snapshot = this.snapshot()
    this.saveChain = this.saveChain.catch(() => undefined).then(() => this.storage.save(snapshot))
    // Observe timer-driven failures immediately; IPC callers still receive the rejected promise.
    void this.saveChain.catch((reason: unknown) => {
      this.state.lastError = `本地保存失败：${reason instanceof Error ? reason.message : String(reason)}`
      for (const listener of this.listeners) listener(this.snapshot())
    })
    await this.saveChain
    for (const listener of this.listeners) listener(snapshot)
  }
}

function createLog(level: TaskLog['level'], message: string): TaskLog {
  return { id: randomUUID(), time: new Date().toISOString(), level, message }
}

function createOutput(task: MockTask, now: string): MockOutput {
  return {
    id: randomUUID(),
    taskId: task.id,
    title: task.title,
    status: task.status === 'quality_issue' ? 'quality_issue' : 'completed',
    createdAt: now,
    durationLabel: task.scenario === 'quality_issue' ? '04:18' : '03:42',
    resolution: '1920 × 1080',
    mock: true
  }
}

function normalizePersistedDiagnostics(state: AppState): void {
  for (const task of state.tasks) {
    if (task.source !== 'real') continue
    const engine = appendBoundedStderr('', task.real.stderr, task.real.stderrTruncated ?? false)
    task.real.stderr = engine.text
    task.real.stderrTruncated = engine.truncated
    if (!task.preparation) continue
    const stdout = appendBoundedDiagnosticText('', task.preparation.stdout, task.preparation.stdoutTruncated ?? false)
    task.preparation.stdout = stdout.text
    task.preparation.stdoutTruncated = stdout.truncated
    const preparation = appendBoundedStderr('', task.preparation.stderr, task.preparation.stderrTruncated ?? false)
    task.preparation.stderr = preparation.text
    task.preparation.stderrTruncated = preparation.truncated
  }
}
