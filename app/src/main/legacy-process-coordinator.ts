import { win32 } from 'node:path'
import type { ProcessIdentityExpectation, ProcessInspection, ProcessInspector } from '../engine/process-inspector'
import type { ProcessTreeTerminator } from '../engine/process-tree-terminator'
import type { RealTask } from '../shared/types'

export type LegacyInterruptionReason = 'missing_pid' | 'process_missing' | 'process_mismatch' | 'process_identity_unknown'
export type LegacyRecoveryDecision =
  | { outcome: 'verified'; pid: number }
  | { outcome: 'interrupted'; reason: LegacyInterruptionReason }
export type LegacyStopDecision = { outcome: 'stopped' } | { outcome: 'interrupted'; reason: LegacyInterruptionReason }

interface VerifiedLegacyProcess {
  taskId: string
  pid: number
  expected: ProcessIdentityExpectation
  inspected: Extract<ProcessInspection, { outcome: 'match' }>['fact']
  verifiedAt: string
}

export class LegacyProcessCoordinator {
  private readonly verified = new Map<string, VerifiedLegacyProcess>()
  private readonly stopPromises = new Map<string, Promise<LegacyStopDecision>>()

  constructor(
    private readonly inspector: ProcessInspector,
    private readonly terminator: ProcessTreeTerminator,
    private readonly now: () => string = () => new Date().toISOString()
  ) {}

  async recover(task: RealTask): Promise<LegacyRecoveryDecision> {
    const target = recoveryTarget(task)
    if ('reason' in target) return { outcome: 'interrupted', reason: target.reason }
    const inspection = await this.inspector.inspect(target.pid, target.expected)
    if (inspection.outcome !== 'match') return interruptionFor(inspection)!
    this.verified.set(task.id, {
      taskId: task.id,
      pid: target.pid,
      expected: target.expected,
      inspected: inspection.fact,
      verifiedAt: this.now()
    })
    return { outcome: 'verified', pid: target.pid }
  }

  getVerifiedTaskId(): string | null { return this.verified.keys().next().value ?? null }
  isVerified(taskId: string): boolean { return this.verified.has(taskId) }

  stop(taskId: string): Promise<LegacyStopDecision> {
    const pending = this.stopPromises.get(taskId)
    if (pending) return pending
    const attempt = this.stopVerified(taskId)
    this.stopPromises.set(taskId, attempt)
    void attempt.then(
      () => { if (this.stopPromises.get(taskId) === attempt) this.stopPromises.delete(taskId) },
      () => { if (this.stopPromises.get(taskId) === attempt) this.stopPromises.delete(taskId) }
    )
    return attempt
  }

  private async stopVerified(taskId: string): Promise<LegacyStopDecision> {
    const record = this.verified.get(taskId)
    if (!record) throw new Error('遗留进程尚未核实，拒绝停止')
    const before = await this.inspector.inspect(record.pid, record.expected)
    const interrupted = interruptionFor(before)
    if (interrupted) {
      this.verified.delete(taskId)
      return interrupted
    }

    let result
    try {
      result = await this.terminator.terminate(record.pid)
    } catch (error) {
      let after: ProcessInspection
      try { after = await this.inspector.inspect(record.pid, record.expected) } catch (inspectionError) {
        after = { outcome: 'unknown', detail: `终止失败后无法重新核实进程：${message(inspectionError)}` }
      }
      if (after.outcome === 'match') throw error
      this.verified.delete(taskId)
      return interruptionFor(after) ?? { outcome: 'interrupted', reason: 'process_identity_unknown' }
    }
    this.verified.delete(taskId)
    return result.outcome === 'terminated'
      ? { outcome: 'stopped' }
      : { outcome: 'interrupted', reason: 'process_missing' }
  }
}

function recoveryTarget(task: RealTask):
  | { pid: number; expected: ProcessIdentityExpectation }
  | { reason: LegacyInterruptionReason } {
  if (task.preparation?.status === 'preparing') {
    if (!task.preparation.pid) return { reason: 'missing_pid' }
    if (!task.preparation.taskRoot.trim()) return { reason: 'process_identity_unknown' }
    return {
      pid: task.preparation.pid,
      expected: { requiredSequences: [
        ['tools\\prepare_sample.py'],
        ['--work-root', win32.join(task.preparation.taskRoot, 'work')]
      ] }
    }
  }

  const association = task.real.association
  if (!association.pid) return { reason: 'missing_pid' }
  if (association.command !== 'run' || !association.workRoot?.trim() || !association.article?.trim()) {
    return { reason: 'process_identity_unknown' }
  }
  return {
    pid: association.pid,
    expected: { requiredSequences: [
      ['-m', 'pipeline.engine', 'run'],
      ['--work-root', association.workRoot],
      ['--article', association.article]
    ] }
  }
}

function interruptionFor(inspection: ProcessInspection): Extract<LegacyRecoveryDecision, { outcome: 'interrupted' }> | null {
  if (inspection.outcome === 'match') return null
  if (inspection.outcome === 'not_running') return { outcome: 'interrupted', reason: 'process_missing' }
  if (inspection.outcome === 'mismatch') return { outcome: 'interrupted', reason: 'process_mismatch' }
  return { outcome: 'interrupted', reason: 'process_identity_unknown' }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
