import type { TaskStatus } from '../shared/types'

export const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: '待开始',
  queued: '等待中',
  running: '运行中',
  completed: '已完成',
  quality_issue: '出片有问题',
  failed: '失败',
  stopped: '已停止',
  interrupted: '已中断'
}
