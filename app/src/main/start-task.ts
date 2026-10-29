import type { EnvironmentReadinessService } from './environment-readiness'
import type { TaskManager } from './task-manager'

export async function startTaskWithReadiness(manager: TaskManager, taskId: string,
  readiness: Pick<EnvironmentReadinessService, 'check'>): Promise<void> {
  if (manager.snapshot().tasks.find(task => task.id === taskId)?.source === 'real') {
    const result = await readiness.check()
    if (result.status !== 'ready') {
      throw new Error(`运行环境未就绪：${result.checks.filter(check => check.status === 'blocked').map(check => check.message).join('；')}`)
    }
  }
  // Keep reservation, concurrency checks and all P3 state transitions in TaskManager.
  await manager.startTask(taskId)
}
