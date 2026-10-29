export const QUIT_BUTTONS = ['停止任务并退出', '取消'] as const

export interface QuitCoordinatorManager {
  getActiveRealTaskId(): string | null
  stopTask(taskId: string): Promise<void>
  close(): Promise<void>
}

export interface QuitCoordinatorActions {
  prompt(): Promise<'stop-and-quit' | 'cancel'>
  showError(title: string, message: string): void
  quit(): void
}

export type QuitResult = 'quit' | 'cancelled' | 'failed'

export class QuitCoordinator {
  private allowed = false
  private pending: Promise<QuitResult> | null = null

  constructor(
    private readonly manager: QuitCoordinatorManager,
    private readonly actions: QuitCoordinatorActions
  ) {}

  isQuitAllowed(): boolean { return this.allowed }

  requestQuit(): Promise<QuitResult> {
    if (this.allowed) return Promise.resolve('quit')
    if (this.pending) return this.pending
    const request = this.run()
    this.pending = request
    void request.finally(() => {
      if (this.pending === request && !this.allowed) this.pending = null
    })
    return request
  }

  private async run(): Promise<QuitResult> {
    const taskId = this.manager.getActiveRealTaskId()
    if (taskId) {
      const choice = await this.actions.prompt()
      if (choice === 'cancel') return 'cancelled'
      try {
        await this.manager.stopTask(taskId)
      } catch (error) {
        this.actions.showError('无法安全退出', `真实任务停止失败，APP 已保持打开：${message(error)}`)
        return 'failed'
      }
    }

    try {
      await this.manager.close()
    } catch (error) {
      this.actions.showError('无法安全退出', `最终状态保存失败，APP 已保持打开：${message(error)}`)
      return 'failed'
    }
    this.allowed = true
    this.actions.quit()
    return 'quit'
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
