import type { CheckReport, EngineEvent, InputProblem, InputResolvedEvent, RunDoneEvent } from './protocol'
import { stepsFor, type ContentType } from './steps'

// 一次运行的界面状态：只由引擎事件推出来（不解析 message 文本）。

export type StepState = 'waiting' | 'running' | 'retrying' | 'done' | 'skipped' | 'failed'

export interface StepView {
  id: string
  name: string
  state: StepState
  progress: { done: number; total: number; unit: string; label: string } | null
}

export interface RunView {
  contentType: ContentType | null
  resolved: InputResolvedEvent | null
  problems: InputProblem[] | null
  steps: StepView[]
  currentStep: string | null
  spentCny: number
  retry: { attempt: number; waitSec: number; service: string } | null
  failure: { step: string | null; errorKind: string; message: string; service: string | null } | null
  runDone: RunDoneEvent | null
  report: CheckReport | null
  // 第一条 / 最近一条事件的时间（引擎给的 time），用来算"已用时"
  firstAt: string | null
  lastAt: string | null
}

export function initialRunView(contentType: ContentType | null = null): RunView {
  return {
    contentType, resolved: null, problems: null, currentStep: null, spentCny: 0, retry: null, failure: null, runDone: null, report: null,
    firstAt: null, lastAt: null,
    steps: stepsFor(contentType).map(step => ({ id: step.id, name: step.name, state: 'waiting', progress: null }))
  }
}

// APP4：引擎这次没发类型（S0 被跳过）时，用账本 / S0 结果补上；已经知道类型就不动
export function fillContentType(view: RunView, type: ContentType | null): RunView {
  return view.contentType ? view : withType(view, type)
}

function withType(view: RunView, type: ContentType | null): RunView {
  if (!type || type === view.contentType) return view
  const previous = new Map(view.steps.map(step => [step.id, step]))
  return { ...view, contentType: type, steps: stepsFor(type).map(step => previous.get(step.id) ?? { id: step.id, name: step.name, state: 'waiting', progress: null }) }
}

function updateStep(view: RunView, id: string, patch: Partial<StepView>): RunView {
  if (!view.steps.some(step => step.id === id)) return view
  return { ...view, steps: view.steps.map(step => step.id === id ? { ...step, ...patch } : step) }
}

const money = (value: unknown): number => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 0

export function reduceRunView(view: RunView, event: EngineEvent): RunView {
  const timed = typeof event.time === 'string' ? { ...view, firstAt: view.firstAt ?? event.time, lastAt: event.time } : view
  return reduceEvent(timed, event)
}

function reduceEvent(view: RunView, event: EngineEvent): RunView {
  switch (event.event) {
    case 'input_resolved':
      return { ...withType(view, event.content_type), resolved: event }
    case 'input_problems':
      return { ...view, problems: event.problems }
    case 'step_start':
      return { ...updateStep(view, event.step, { state: 'running' }), currentStep: event.step, retry: null }
    case 'step_skip':
      return updateStep(view, event.step, { state: 'skipped' })
    case 'step_progress': {
      const progress = { done: event.done, total: event.total, unit: event.unit, label: event.retry ? '' : event.message }
      if (event.retry) {
        return { ...updateStep(view, event.step, { state: 'retrying', progress: view.steps.find(step => step.id === event.step)?.progress ?? progress }),
          currentStep: event.step, retry: { attempt: event.retry.attempt, waitSec: event.retry.wait_sec, service: event.retry.service } }
      }
      return { ...updateStep(view, event.step, { state: 'running', progress }), currentStep: event.step, retry: null }
    }
    case 'step_done': {
      // S0 定下类型后，流程条按真实类型换成 10 步或 11 步
      const typed = event.step === 'S0' && typeof (event as { content_type?: unknown }).content_type === 'string'
        ? withType(view, (event as unknown as { content_type: ContentType }).content_type) : view
      return { ...updateStep(typed, event.step, { state: 'done' }), spentCny: Math.round((view.spentCny + money(event.estimated_cny)) * 100) / 100, retry: null }
    }
    case 'step_fail':
      return { ...updateStep(view, event.step, { state: 'failed' }), failure: { step: event.step, errorKind: event.error_kind, message: event.message, service: event.service ?? null }, retry: null }
    case 'run_done': {
      const failure = event.error_kind && !view.failure ? { step: view.currentStep, errorKind: event.error_kind, message: event.message, service: event.service ?? null } : view.failure
      // 被停止时正在跑的那一步标成"已停"
      const stopped = event.error_kind === 'interrupted' && view.currentStep
        ? updateStep(view, view.currentStep, { state: 'failed' }) : view
      return { ...stopped, failure, runDone: event, retry: null }
    }
    default:
      return view
  }
}

export function reduceAll(events: EngineEvent[], contentType: ContentType | null = null): RunView {
  return events.reduce(reduceRunView, initialRunView(contentType))
}

export type RunPhase = 'idle' | 'running' | 'retrying' | 'failed' | 'stopped' | 'missing_images' | 'done_pass' | 'done_review' | 'done_red'

export function runPhase(view: RunView): RunPhase {
  const done = view.runDone
  if (!done) return view.retry ? 'retrying' : (view.currentStep ? 'running' : 'idle')
  if (done.exit_code === 3) return 'stopped'
  if (done.exit_code >= 2) return 'failed'
  const reds = view.report?.items.filter(item => item.level === 'red') ?? []
  if (reds.some(item => item.kind === 'illustration_missing')) return 'missing_images'
  if (done.exit_code === 1 || view.report?.verdict === 'fail') return 'done_red'
  if (view.report?.verdict === 'review') return 'done_review'
  return 'done_pass'
}

export function stepIndex(view: RunView): number {
  const index = view.steps.findIndex(step => step.id === view.currentStep)
  return index < 0 ? 0 : index + 1
}
