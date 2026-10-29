import type { Task } from '../shared/types'

export interface EngineAdapter<T extends Task = Task> {
  start(task: T): void
  stop(taskId: string): void | Promise<void>
  getRunningTaskId(): string | null
  dispose(): void
}
