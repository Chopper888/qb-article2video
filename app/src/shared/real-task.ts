import type { RealRunData, RealTask } from './types'

export const REAL_NOT_CONNECTED = '当前 Real 仅开放示例篇开发联调样本；Retry / Resume 尚未实现'
export const LEGACY_RUNNING_MESSAGE = '检测到上次任务仍在后台运行'

export function emptyRealRun(): RealRunData {
  return {
    observationTimeSource: 'app',
    association: { workRoot: null, article: null, processInstanceId: null },
    steps: [], events: [], diagnostics: [], stderr: '', stderrTruncated: false, runDone: null,
    processExit: null, missingRunDone: null, deliveryPath: null, stepFailure: null
  }
}

export function createRealTaskRecord(id: string, title: string, now: string): RealTask {
  return {
    id, title, source: 'real', status: 'pending', progress: 0, currentStep: null,
    createdAt: now, updatedAt: now, startedAt: null, finishedAt: null,
    error: null, logs: [], real: emptyRealRun()
  }
}
