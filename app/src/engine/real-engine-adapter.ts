import { randomUUID } from 'node:crypto'
import type { EngineEvent, ProcessExitFact, RealRunData, RealTask } from '../shared/types'
import { REAL_NOT_CONNECTED } from '../shared/real-task'
import type { EngineAdapter } from './engine-adapter'
import { RealRunSession } from './real-run-session'
import { buildEngineCommand, type EngineRequest } from './real-engine-command'
import type { EngineResources } from './engine-resources'
import type { ProcessTransport } from './process-transport'
import { TaskkillProcessTreeTerminator, type ProcessTreeTerminator } from './process-tree-terminator'

export interface RecordedRun {
  records: Array<{ channel: 'stdout'; data: string | Uint8Array } | { channel: 'stderr'; data: string }>
  exit: ProcessExitFact
}

export interface RealProcessOptions {
  transport: ProcessTransport
  terminator?: ProcessTreeTerminator
  request(task: RealTask): EngineRequest
  resources: EngineResources
  environment?: NodeJS.ProcessEnv
  allowRun?: boolean
}

interface ActiveProcessRun {
  taskId: string
  association: RealRunData['association']
  session: RealRunSession
  pidReady: Promise<number>
  resolvePid(pid: number): void
  completion: Promise<void>
  resolveCompletion(): void
  pendingExit: ProcessExitFact | null
  settled: boolean
  stopDecisionPending: boolean
  terminationConfirmed: boolean
  stopPromise: Promise<void> | null
}

// Normal APP construction remains disconnected. Controlled callers explicitly inject
// process configuration; legacy in-memory replay remains available for R2A tests.
export class RealEngineAdapter implements EngineAdapter<RealTask> {
  private active: string | null = null
  private activeRun: ActiveProcessRun | null = null
  private disposed = false
  constructor(private readonly emit: (event: EngineEvent) => void, private readonly source?: RecordedRun | RealProcessOptions) {}

  start(task: RealTask): void {
    if (task.source !== 'real') throw new Error('Real adapter 只接受 Real 任务')
    if (this.disposed) throw new Error('Real adapter disposed')
    if (!this.source) throw new Error(REAL_NOT_CONNECTED)
    if (this.active) throw new Error('Real 准备层仅允许单任务')
    if (!('records' in this.source)) {
      this.startProcess(task, this.source)
      return
    }
    this.active = task.id
    try {
      const session = new RealRunSession(task)
      this.publish(session)
      for (const record of this.source.records) {
        if (record.channel === 'stdout') session.stdout(record.data)
        else session.stderr(record.data)
        this.publish(session, undefined, record.channel === 'stderr')
      }
      session.finish(this.source.exit)
      this.publish(session)
    } finally { this.active = null }
  }

  stop(taskId: string): Promise<void> {
    if (!this.active) return Promise.resolve()
    if (this.active !== taskId) return Promise.reject(new Error('拒绝停止不属于当前活动 RealTask 的进程'))
    const run = this.activeRun
    if (!run) return Promise.reject(new Error('当前 Real 记录没有可停止的活动进程'))
    if (run.stopPromise) return run.stopPromise
    run.stopDecisionPending = true
    const attempt = this.stopActiveRun(run)
    run.stopPromise = attempt
    return attempt
  }
  getRunningTaskId(): string | null { return this.active }
  // Disposing an observer is not a successful stop; retain active facts until close.
  dispose(): void { this.disposed = true }

  private startProcess(task: RealTask, options: RealProcessOptions): void {
    const request = options.request(structuredClone(task))
    if (request.command !== 'status' && request.command !== 'plan' && !(request.command === 'run' && options.allowRun)) throw new Error('默认仅允许 status/plan；run 需要受控样本 Main 服务')
    const command = buildEngineCommand(request, options.resources, options.environment)
    const association: RealRunData['association'] = {
      workRoot: request.workRoot, article: request.article, processInstanceId: randomUUID(), pid: null,
      engineRoot: command.cwd, engineCommit: options.resources.engineCommit, command: request.command
    }
    const session = new RealRunSession({ ...task, real: { ...task.real, association } })
    this.active = task.id
    let resolvePid!: (pid: number) => void
    let resolveCompletion!: () => void
    const run: ActiveProcessRun = {
      taskId: task.id,
      association,
      session,
      pidReady: new Promise<number>((resolve) => { resolvePid = resolve }),
      resolvePid: (pid) => resolvePid(pid),
      completion: new Promise<void>((resolve) => { resolveCompletion = resolve }),
      resolveCompletion: () => resolveCompletion(),
      pendingExit: null,
      settled: false,
      stopDecisionPending: false,
      terminationConfirmed: false,
      stopPromise: null
    }
    this.activeRun = run
    const publish = (): void => this.publish(session, association)
    const finish = (fact: ProcessExitFact): void => {
      run.pendingExit = fact
      run.resolveCompletion()
      if (run.terminationConfirmed) this.settleProcessRun(run, true)
      else if (!run.stopDecisionPending || session.snapshot().real.runDone !== null) this.settleProcessRun(run, false)
    }
    publish()
    try {
      void options.transport.execute(command, {
        stdout: (chunk) => { session.stdout(chunk); publish() },
        stderr: (text) => { session.stderr(text); this.publish(session, association, true) },
        spawned: (pid) => {
          association.pid = pid
          if (pid !== null && Number.isInteger(pid) && pid > 0) run.resolvePid(pid)
          publish()
        }
      }).then(finish, (error: unknown) => finish({ code: null, signal: null, error: error instanceof Error ? error.message : String(error) }))
    } catch (error) {
      finish({ code: null, signal: null, error: error instanceof Error ? error.message : String(error) })
    }
  }

  private async stopActiveRun(run: ActiveProcessRun): Promise<void> {
    try {
      const pid = await Promise.race([run.pidReady, run.completion.then(() => null)])
      if (pid === null) {
        run.stopDecisionPending = false
        this.settleProcessRun(run, false)
        return
      }
      if (this.activeRun !== run) return
      const options = this.source && !('records' in this.source) ? this.source : null
      const terminator = options?.terminator ?? new TaskkillProcessTreeTerminator()
      try {
        await terminator.terminate(pid)
      } catch (error) {
        if (this.activeRun !== run) return
        run.stopDecisionPending = false
        this.settleProcessRun(run, false)
        throw error
      }
      // A formal natural terminal that won the race keeps its natural result.
      if (this.activeRun !== run) return
      run.terminationConfirmed = true
      run.stopDecisionPending = false
      this.settleProcessRun(run, true)
      await run.completion
    } finally {
      if (this.activeRun === run && !run.terminationConfirmed) run.stopPromise = null
    }
  }

  private settleProcessRun(run: ActiveProcessRun, stopped: boolean): void {
    if (run.settled || !run.pendingExit) return
    run.settled = true
    if (stopped) run.session.stop(run.pendingExit)
    else run.session.finish(run.pendingExit)
    if (this.activeRun === run) {
      this.activeRun = null
      this.active = null
    }
    this.publish(run.session, run.association)
  }

  private publish(session: RealRunSession, association?: RealRunData['association'], diagnosticsOnly = false): void {
    const task = session.snapshot()
    if (association) task.real.association = { ...association }
    this.emit({
      taskId: task.id,
      status: task.status,
      currentStep: task.currentStep,
      error: task.error,
      real: task.real,
      ...(diagnosticsOnly ? { diagnosticsOnly: true } : {})
    })
  }
}
