import type { EngineEvent, MockTask } from '../shared/types'
import type { EngineAdapter } from './engine-adapter'

const STEPS = [
  '内容类型判断',
  '口播',
  '分组',
  '配音',
  '对齐',
  '图片定位',
  '编排',
  '场景构建',
  '成片渲染'
] as const

interface ActiveRun {
  task: MockTask
  stepIndex: number
  timer: ReturnType<typeof setTimeout> | null
}

export class MockEngineAdapter implements EngineAdapter<MockTask> {
  private active: ActiveRun | null = null
  private readonly queue: MockTask[] = []

  constructor(
    private readonly emit: (event: EngineEvent) => void,
    private readonly tickMs = 550
  ) {}

  start(task: MockTask): void {
    if (this.active?.task.id === task.id || this.queue.some((queued) => queued.id === task.id)) return
    if (this.active) {
      this.queue.push(task)
      this.emit({
        taskId: task.id,
        status: 'queued',
        progress: 0,
        currentStep: null,
        log: { level: 'warning', message: '当前有 Mock 任务运行，已进入等待队列' }
      })
      return
    }
    this.begin(task)
  }

  stop(taskId: string): void {
    if (this.active?.task.id === taskId) {
      if (this.active.timer) clearTimeout(this.active.timer)
      this.emit({
        taskId,
        status: 'stopped',
        currentStep: null,
        log: { level: 'warning', message: '用户已停止 Mock 任务' }
      })
      this.active = null
      this.startNext()
      return
    }

    const index = this.queue.findIndex((task) => task.id === taskId)
    if (index >= 0) {
      this.queue.splice(index, 1)
      this.emit({
        taskId,
        status: 'stopped',
        currentStep: null,
        log: { level: 'warning', message: '已取消等待中的 Mock 任务' }
      })
    }
  }

  getRunningTaskId(): string | null {
    return this.active?.task.id ?? null
  }

  dispose(): void {
    if (this.active?.timer) clearTimeout(this.active.timer)
    this.active = null
    this.queue.length = 0
  }

  private begin(task: MockTask): void {
    this.active = { task, stepIndex: 0, timer: null }
    this.emit({
      taskId: task.id,
      status: 'running',
      progress: 0,
      currentStep: STEPS[0],
      error: null,
      log: { level: 'info', message: 'Mock 任务开始' }
    })
    this.emit({
      taskId: task.id,
      log: { level: 'info', message: `步骤开始：${STEPS[0]}` }
    })
    this.scheduleTick()
  }

  private scheduleTick(): void {
    if (!this.active) return
    this.active.timer = setTimeout(() => this.advance(), this.tickMs)
  }

  private advance(): void {
    if (!this.active) return
    const { task, stepIndex } = this.active
    const step = STEPS[stepIndex]
    if (!step) return

    if (task.scenario === 'failure' && stepIndex === 5) {
      this.emit({
        taskId: task.id,
        status: 'failed',
        progress: 62,
        currentStep: step,
        error: 'Mock Engine：模拟执行失败（图片定位阶段）',
        log: { level: 'error', message: '步骤失败：模拟输入数据不完整' }
      })
      this.finishCurrent()
      return
    }

    const progress = Math.round(((stepIndex + 1) / STEPS.length) * 100)
    this.emit({
      taskId: task.id,
      progress,
      log: { level: 'success', message: `步骤完成：${step}` }
    })

    const nextIndex = stepIndex + 1
    if (nextIndex >= STEPS.length) {
      const qualityIssue = task.scenario === 'quality_issue'
      this.emit({
        taskId: task.id,
        status: qualityIssue ? 'quality_issue' : 'completed',
        progress: 100,
        currentStep: null,
        log: {
          level: qualityIssue ? 'warning' : 'success',
          message: qualityIssue
            ? 'Mock 任务完成，但模拟检查发现出片问题'
            : 'Mock 任务完成，已生成模拟输出'
        }
      })
      this.finishCurrent()
      return
    }

    this.active.stepIndex = nextIndex
    const nextStep = STEPS[nextIndex]
    this.emit({
      taskId: task.id,
      currentStep: nextStep,
      log: { level: 'info', message: `步骤开始：${nextStep}` }
    })
    this.scheduleTick()
  }

  private finishCurrent(): void {
    this.active = null
    this.startNext()
  }

  private startNext(): void {
    const next = this.queue.shift()
    if (next) this.begin(next)
  }
}
