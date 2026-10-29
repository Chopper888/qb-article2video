import type { ProcessExitFact, RealStep, RealTask, TaskStatus } from '../shared/types'
import { emptyRealRun } from '../shared/real-task'
import { JsonlParser } from './jsonl-parser'
import type { JsonlResult } from './jsonl-parser'
import { appendBoundedStderr } from '../shared/stderr-tail'

const exitStatuses: Record<number, TaskStatus> = { 0: 'completed', 1: 'quality_issue', 2: 'failed', 3: 'stopped', 4: 'failed' }

// Pure observation/mapping. R2A feeds static data only; no process or file APIs here.
export class RealRunSession {
  private readonly parser = new JsonlParser()
  private readonly task: RealTask
  private finished = false
  private protocolError = false

  constructor(task: RealTask, private readonly now: () => string = () => new Date().toISOString()) {
    this.task = structuredClone(task)
    this.task.real = { ...emptyRealRun(), association: { ...task.real.association } }
    this.task.status = 'running'
    this.task.startedAt = now()
    this.task.finishedAt = null
    this.task.error = null
    this.task.currentStep = null
  }

  snapshot(): RealTask { return structuredClone(this.task) }

  stdout(chunk: string | Uint8Array): void {
    if (this.finished) throw new Error('执行观察已结束')
    this.accept(this.parser.push(chunk))
  }

  stderr(text: string): void {
    if (this.finished) throw new Error('执行观察已结束')
    const bounded = appendBoundedStderr(this.task.real.stderr, text, this.task.real.stderrTruncated ?? false)
    this.task.real.stderr = bounded.text
    this.task.real.stderrTruncated = bounded.truncated
  }

  finish(fact: ProcessExitFact): void {
    if (this.finished) return
    this.accept(this.parser.end())
    this.finished = true
    const data = this.task.real
    data.processExit = structuredClone(fact)
    data.missingRunDone = data.runDone === null
    if (!data.runDone) this.diagnose('missing_run_done', '进程结束但没有正式 run_done；原始退出事实已保留')
    else {
      if (fact.code !== data.runDone.exitCode) this.diagnose('exit_mismatch', '协议/执行异常：run_done 与进程退出码不一致')
      if (!(data.runDone.exitCode in exitStatuses)) this.diagnose('unknown_exit_code', '协议/执行异常：未知终结码')
      if ([0, 1].includes(data.runDone.exitCode) && data.steps.some((step) => step.status === 'failed')) {
        this.diagnose('failed_step_with_success', '协议/执行异常：失败步骤与成功终结不一致')
      }
    }
    if (fact.signal || fact.error) this.diagnose('process_error', fact.error || `进程信号：${fact.signal}`)
    this.task.status = this.protocolError ? 'failed' : exitStatuses[data.runDone!.exitCode] ?? 'failed'
    if (this.task.status === 'failed') {
      this.task.error = data.stepFailure || data.steps.find((step) => step.status === 'failed')?.message ||
        data.runDone?.message || data.stderr || fact.error || data.diagnostics.at(-1)?.message || '执行异常'
    }
    this.task.finishedAt = this.now()
    this.task.updatedAt = this.task.finishedAt
  }

  stop(fact: ProcessExitFact): void {
    if (this.finished) return
    this.accept(this.parser.end())
    this.finished = true
    this.task.real.processExit = structuredClone(fact)
    this.task.real.missingRunDone = this.task.real.runDone === null
    this.task.status = 'stopped'
    this.task.error = null
    this.task.currentStep = null
    this.task.finishedAt = this.now()
    this.task.updatedAt = this.task.finishedAt
  }

  private diagnose(kind: string, message: string, raw: string | null = null): void {
    this.protocolError = true
    this.task.real.diagnostics.push({ kind, message, raw })
  }

  private accept(results: JsonlResult[]): void {
    for (const result of results) {
      if (result.kind === 'diagnostic') { this.diagnose('invalid_stdout', result.message, result.raw); continue }
      const event = result.event
      const data = this.task.real
      data.events.push(structuredClone(event))
      if (result.kind === 'unknown') {
        data.diagnostics.push({ kind: 'unknown_event', message: `未识别事件：${event.event}（仅保留）`, raw: result.raw })
        continue
      }
      if (data.runDone) { this.diagnose('event_after_run_done', '正式终结后仍有事件', result.raw); continue }
      if (event.event === 'run_done') {
        data.runDone = { exitCode: event.exit_code!, message: event.message, ...(event.reason === undefined ? {} : { reason: event.reason }) }
        if (typeof event.delivery_path === 'string' && event.delivery_path.trim()) data.deliveryPath = event.delivery_path
        // Wait for the independent process-end fact before publishing a terminal state/output.
      } else {
        const status: RealStep['status'] = event.event === 'step_start' ? 'running' : event.event === 'step_done' ? 'done' : event.event === 'step_skip' ? 'skipped' : 'failed'
        let step = data.steps.find((item) => item.step === event.step)
        if (!step) {
          step = { step: event.step!, name: null, status, message: '', outputs: [] }
          data.steps.push(step)
        }
        if (event.name !== undefined) step.name = event.name
        step.status = status
        step.message = event.message
        if (event.outputs !== undefined) step.outputs = [...event.outputs]
        this.task.currentStep = event.step
        if (event.event === 'step_fail') data.stepFailure = event.message
      }
      this.task.updatedAt = this.now()
      this.task.logs.push({ id: `observation-${data.events.length}`, time: this.now(), level: event.event === 'step_fail' ? 'error' : 'info', message: `[${event.event}${event.step ? ` ${event.step}` : ''}] ${event.message}` })
      this.task.logs = this.task.logs.slice(-200)
    }
  }
}
