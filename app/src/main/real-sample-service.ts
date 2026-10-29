import { mkdir } from 'node:fs/promises'
import { win32 } from 'node:path'
import { StringDecoder } from 'node:string_decoder'
import type { EngineEvent, RealTask, SamplePreparation, StateStorage } from '../shared/types'
import type { EngineAdapter } from '../engine/engine-adapter'
import { RealEngineAdapter } from '../engine/real-engine-adapter'
import { MockEngineAdapter } from '../engine/mock-engine-adapter'
import { NodeProcessTransport, type ProcessTransport } from '../engine/process-transport'
import { TaskkillProcessTreeTerminator, type ProcessTreeTerminator } from '../engine/process-tree-terminator'
import { WindowsProcessInspector, type ProcessInspector } from '../engine/process-inspector'
import { buildEngineEnvironment, type ProcessCommand } from '../engine/real-engine-command'
import type { SampleResources } from '../engine/engine-resources'
import { LegacyProcessCoordinator } from './legacy-process-coordinator'
import { TaskManager } from './task-manager'
import { RuntimeWorkspace, resolveRuntimeDirectory, type RuntimePreflight } from './runtime-workspace'
import { appendBoundedDiagnosticText, appendBoundedStderr } from '../shared/stderr-tail'

const ARTICLE = '001-示例篇'

export function samplePaths(id: string, integrationRoot: string, runtimeDir = resolveRuntimeDirectory()) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) throw new Error('无效 APP task id')
  const taskRoot = win32.join(integrationRoot, id)
  return { taskRoot, workRoot: win32.join(taskRoot, 'work'), deliveryRoot: win32.join(taskRoot, 'delivery'), tmpDir: win32.join(taskRoot, 'tmp'), runtimeDir }
}

export function buildPrepareCommand(paths: ReturnType<typeof samplePaths>, resources: SampleResources, parent: NodeJS.ProcessEnv = process.env): ProcessCommand {
  return {
    executable: resources.pythonExecutable, cwd: resources.engineRoot, env: buildEngineEnvironment(resources, parent),
    args: ['-B', '-u', 'tools\\prepare_sample.py', '--work-root', paths.workRoot, '--sample', '示例篇', '--name', ARTICLE,
      '--with-runtime', '--runtime-source', resources.runtimeSource, '--sample-source-root', resources.sampleSourceRoot, '--runtime-dir', paths.runtimeDir]
  }
}

// Main-side prerequisite orchestration only. All engine semantics remain in RealRunSession.
interface ActiveSampleRun {
  taskId: string
  paths: ReturnType<typeof samplePaths>
  phase: 'setup' | 'preparing' | 'engine' | 'finished'
  stopRequested: boolean
  stopFailure: Error | null
  stopPromise: Promise<void> | null
  terminationAttempt: Promise<void> | null
  pidReady: Promise<number>
  resolvePid(pid: number): void
  prepareClosed: Promise<void>
  resolvePrepareClosed(): void
  flowDone: Promise<void>
  resolveFlowDone(): void
}

class SampleAdapter implements EngineAdapter<RealTask> {
  private active: ActiveSampleRun | null = null
  private disposed = false
  private readonly engine: RealEngineAdapter

  constructor(
    private readonly emit: (event: EngineEvent) => void,
    private readonly root: string,
    private readonly resources: SampleResources | null,
    private readonly transport: ProcessTransport,
    private readonly terminator: ProcessTreeTerminator,
    private readonly runtimeWorkspace: RuntimePreflight
  ) {
    this.engine = new RealEngineAdapter(event => {
      if (event.status && !['running', 'pending', 'queued'].includes(event.status)) {
        if (this.active?.taskId === event.taskId) this.active.phase = 'finished'
        this.active = null
      }
      emit(event)
    }, resources ? { transport, terminator, resources, allowRun: true, request: task => {
      if (this.active?.taskId !== task.id || !this.active.paths.runtimeDir) throw new Error('Runtime preflight 尚未通过')
      return { command: 'run', ...this.active.paths, article: ARTICLE }
    } } : undefined)
  }

  start(task: RealTask): void {
    if (this.disposed || this.active) throw new Error('Real 单任务服务不可用；不并发')
    if (task.sample !== 'sample-v4.1') throw new Error('仅支持示例篇 v4.1 开发联调样本')
    let resolvePid!: (pid: number) => void
    let resolvePrepareClosed!: () => void
    let resolveFlowDone!: () => void
    const run: ActiveSampleRun = {
      taskId: task.id,
      paths: samplePaths(task.id, this.root, ''),
      phase: 'setup',
      stopRequested: false,
      stopFailure: null,
      stopPromise: null,
      terminationAttempt: null,
      pidReady: new Promise<number>(resolve => { resolvePid = resolve }),
      resolvePid: pid => resolvePid(pid),
      prepareClosed: new Promise<void>(resolve => { resolvePrepareClosed = resolve }),
      resolvePrepareClosed: () => resolvePrepareClosed(),
      flowDone: new Promise<void>(resolve => { resolveFlowDone = resolve }),
      resolveFlowDone: () => resolveFlowDone()
    }
    this.active = run
    void this.prepareThenRun(run, task).finally(() => run.resolveFlowDone())
  }

  getRunningTaskId(): string | null { return this.active?.taskId ?? null }
  stop(id: string): Promise<void> {
    const run = this.active
    if (!run || run.taskId !== id) return Promise.reject(new Error('拒绝停止不属于当前活动 RealTask 的进程'))
    if (run.phase === 'engine') return this.engine.stop(id)
    if (run.stopPromise) return run.stopPromise
    run.stopRequested = true
    run.terminationAttempt = this.terminatePreparation(run)
    const attempt = run.terminationAttempt.then(() => run.flowDone)
    run.stopPromise = attempt
    return attempt
  }
  dispose(): void { this.disposed = true; this.engine.dispose() }

  private async prepareThenRun(run: ActiveSampleRun, task: RealTask): Promise<void> {
    const paths = run.paths
    const preparation: SamplePreparation = { status: 'preparing', taskRoot: paths.taskRoot, pid: null, stdout: '', stdoutTruncated: false, stderr: '', stderrTruncated: false, processExit: null }
    const publish = (diagnosticsOnly = false): void => this.emit({
      taskId: task.id,
      status: 'running',
      preparation: structuredClone(preparation),
      ...(diagnosticsOnly ? { diagnosticsOnly: true } : {})
    })
    publish()
    try {
      if (!this.resources) throw new Error('发布资源尚未配置；当前不能启动 Real，不会回退到开发机资源')
      paths.runtimeDir = await this.runtimeWorkspace.check()
      await mkdir(this.root, { recursive: true })
      try { await mkdir(paths.taskRoot) } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'EEXIST') throw new Error('任务目录已存在；请创建新任务，不覆盖或删除现场')
        throw error
      }
      await mkdir(paths.tmpDir)
      if (run.stopRequested) {
        run.resolvePrepareClosed()
        await run.terminationAttempt
        preparation.status = 'stopped'
        run.phase = 'finished'
        this.active = null
        this.emit({ taskId: task.id, status: 'stopped', currentStep: null, error: null, preparation })
        return
      }
      const cmd = buildPrepareCommand(paths, this.resources)
      cmd.env.TEMP = paths.tmpDir
      cmd.env.TMP = paths.tmpDir
      const decoder = new StringDecoder('utf8')
      run.phase = 'preparing'
      preparation.processExit = await this.transport.execute(cmd, {
        spawned: pid => {
          preparation.pid = pid
          if (pid !== null && Number.isInteger(pid) && pid > 0) run.resolvePid(pid)
          publish()
        },
        stdout: chunk => {
          const bounded = appendBoundedDiagnosticText(preparation.stdout, decoder.write(Buffer.from(chunk)), preparation.stdoutTruncated ?? false)
          preparation.stdout = bounded.text
          preparation.stdoutTruncated = bounded.truncated
          publish(true)
        },
        stderr: text => {
          const bounded = appendBoundedStderr(preparation.stderr, text, preparation.stderrTruncated ?? false)
          preparation.stderr = bounded.text
          preparation.stderrTruncated = bounded.truncated
          publish(true)
        }
      })
      run.resolvePrepareClosed()
      const stdoutTail = appendBoundedDiagnosticText(preparation.stdout, decoder.end(), preparation.stdoutTruncated ?? false)
      preparation.stdout = stdoutTail.text
      preparation.stdoutTruncated = stdoutTail.truncated
      if (run.stopRequested) {
        try {
          await run.terminationAttempt
          preparation.status = 'stopped'
          run.phase = 'finished'
          this.active = null
          this.emit({ taskId: task.id, status: 'stopped', currentStep: null, error: null, preparation })
        } catch (error) {
          run.stopFailure = error instanceof Error ? error : new Error(String(error))
          throw new Error(`停止失败：${run.stopFailure.message}`)
        }
        return
      }
      if (preparation.processExit.code !== 0 || preparation.processExit.error || preparation.processExit.signal) throw new Error(preparation.stderr || preparation.processExit.error || `prepare exit ${preparation.processExit.code}`)
      preparation.status = 'ready'; publish()
      run.phase = 'engine'
      this.engine.start(task)
    } catch (error) {
      run.resolvePrepareClosed()
      const phase = preparation.status === 'ready' ? '引擎启动前检查失败（APP）' : '样本/运行时准备失败（APP 前置阶段）'
      preparation.status = 'failed'
      run.phase = 'finished'
      this.active = null
      this.emit({ taskId: task.id, status: 'failed', error: `${phase}：${error instanceof Error ? error.message : String(error)}`, preparation })
    }
  }

  private async terminatePreparation(run: ActiveSampleRun): Promise<void> {
    try {
      const pid = await Promise.race([run.pidReady, run.prepareClosed.then(() => null)])
      if (pid === null || this.active !== run || run.phase === 'finished') return
      await this.terminator.terminate(pid)
    } catch (error) {
      run.stopFailure = error instanceof Error ? error : new Error(String(error))
      throw run.stopFailure
    }
  }
}

// Electron IPC and the authorized integration harness share this exact Main entry.
export function createApplicationManager(storage: StateStorage, options: { realTaskRoot: string; resources: SampleResources | null; runtimeWorkspace?: RuntimePreflight; transport?: ProcessTransport; terminator?: ProcessTreeTerminator; inspector?: ProcessInspector; tickMs?: number }): Promise<TaskManager> {
  const terminator = options.terminator ?? new TaskkillProcessTreeTerminator()
  return TaskManager.create(storage, options.tickMs ?? 550, (emit, tickMs) => ({
    mock: new MockEngineAdapter(emit, tickMs),
    real: new SampleAdapter(emit, options.realTaskRoot, options.resources, options.transport ?? new NodeProcessTransport(), terminator, options.runtimeWorkspace ?? new RuntimeWorkspace(options.resources))
  }), new LegacyProcessCoordinator(options.inspector ?? new WindowsProcessInspector(), terminator))
}
