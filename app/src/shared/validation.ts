import type { AppSettings, AppState, CreateTaskInput, RealRunData } from './types'

const scenarios = ['success', 'quality_issue', 'failure']
const statuses = ['pending', 'queued', 'running', 'completed', 'quality_issue', 'failed', 'stopped', 'interrupted']
const interruptionReasons = ['missing_pid', 'process_missing', 'process_mismatch', 'process_identity_unknown']
const realSamples = ['sample-v3.2', 'sample-v4.1']
const directoryKeys = ['workspace', 'output', 'integration', 'logs']
const settingKeys = ['theme', 'defaultPage', 'outputView', 'openDetailAfterCreate', 'directories']
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const only = (value: Record<string, unknown>, keys: string[]): boolean =>
  Object.keys(value).every((key) => keys.includes(key))
const text = (value: unknown): value is string => typeof value === 'string'
const optionalText = (value: unknown): boolean => value === null || text(value)
const oneOf = (value: unknown, choices: string[]): boolean => text(value) && choices.includes(value)
const date = (value: unknown): boolean => text(value) && Number.isFinite(Date.parse(value))
const optionalDate = (value: unknown): boolean => value === null || date(value)

export function isCreateTaskInput(value: unknown): value is CreateTaskInput {
  return record(value) && only(value, ['title', 'scenario']) && text(value.title) &&
    value.title.length <= 200 && oneOf(value.scenario, scenarios)
}

export function isSettingsPatch(value: unknown): value is Partial<AppSettings> {
  if (!record(value) || !only(value, settingKeys)) return false
  if ('theme' in value && !oneOf(value.theme, ['system', 'light', 'dark'])) return false
  if ('defaultPage' in value && !oneOf(value.defaultPage, ['dashboard', 'create', 'tasks', 'outputs', 'settings'])) return false
  if ('outputView' in value && !oneOf(value.outputView, ['list', 'grid'])) return false
  if ('openDetailAfterCreate' in value && typeof value.openDetailAfterCreate !== 'boolean') return false
  if ('directories' in value) {
    const directories = value.directories
    if (!record(directories) || !only(directories, directoryKeys) || !directoryKeys.every((key) => text(directories[key]))) return false
  }
  return true
}

export function isRealRunData(value: unknown): value is RealRunData {
  if (!record(value) || !only(value, ['observationTimeSource', 'association', 'steps', 'events', 'diagnostics', 'stderr', 'stderrTruncated', 'runDone', 'processExit', 'missingRunDone', 'deliveryPath', 'stepFailure'])) return false
  const { association, runDone, processExit } = value
  return value.observationTimeSource === 'app' && record(association) &&
    only(association, ['workRoot', 'article', 'processInstanceId', 'pid', 'engineRoot', 'engineCommit', 'command']) &&
    ['workRoot', 'article', 'processInstanceId'].every((key) => optionalText(association[key])) &&
    (!('pid' in association) || association.pid === null || (typeof association.pid === 'number' && Number.isInteger(association.pid) && association.pid > 0)) &&
    ['engineRoot', 'engineCommit'].every((key) => !(key in association) || text(association[key])) &&
    (!('command' in association) || oneOf(association.command, ['run', 'plan', 'status'])) &&
    Array.isArray(value.steps) && value.steps.every((step: unknown) => record(step) &&
      only(step, ['step', 'name', 'status', 'message', 'outputs']) && text(step.step) && optionalText(step.name) &&
      oneOf(step.status, ['running', 'done', 'skipped', 'failed']) && text(step.message) &&
      Array.isArray(step.outputs) && step.outputs.every(text)) &&
    Array.isArray(value.events) && value.events.every(record) &&
    Array.isArray(value.diagnostics) && value.diagnostics.every((item: unknown) => record(item) &&
      only(item, ['kind', 'message', 'raw']) && text(item.kind) && text(item.message) && optionalText(item.raw)) &&
    text(value.stderr) && (!('stderrTruncated' in value) || typeof value.stderrTruncated === 'boolean') && optionalText(value.deliveryPath) && optionalText(value.stepFailure) &&
    (value.missingRunDone === null || typeof value.missingRunDone === 'boolean') &&
    (runDone === null || (record(runDone) && only(runDone, ['exitCode', 'message', 'reason']) && Number.isInteger(runDone.exitCode) && text(runDone.message) &&
      (!('reason' in runDone) || text(runDone.reason)))) &&
    (processExit === null || (record(processExit) && only(processExit, ['code', 'signal', 'error']) &&
      (processExit.code === null || Number.isInteger(processExit.code)) && optionalText(processExit.signal) && optionalText(processExit.error)))
}

export function isAppState(value: unknown): value is AppState {
  if (!record(value) || !only(value, ['schemaVersion', 'tasks', 'outputs', 'settings', 'lastError'])) return false
  const settings = value.settings
  if (value.schemaVersion !== 1 || !Array.isArray(value.tasks) || !Array.isArray(value.outputs) ||
    !optionalText(value.lastError) || !record(settings) || !isSettingsPatch(settings) ||
    !settingKeys.every((key) => key in settings)) return false
  const tasksValid = value.tasks.every((task: unknown) => record(task) &&
    only(task, ['id', 'title', 'source', ...(task.source === 'real' ? ['real', 'sample', 'preparation', 'interruptionReason'] : ['scenario']), 'status', 'progress', 'currentStep', 'createdAt', 'updatedAt', 'startedAt', 'finishedAt', 'error', 'logs']) &&
    text(task.id) && text(task.title) && ((task.source === 'mock' && oneOf(task.scenario, scenarios)) ||
      (task.source === 'real' && isRealRunData(task.real) &&
        (!('sample' in task) || oneOf(task.sample, realSamples)) &&
        (!('interruptionReason' in task) || oneOf(task.interruptionReason, interruptionReasons)) &&
        (task.status !== 'interrupted' || oneOf(task.interruptionReason, interruptionReasons)) &&
        (!('preparation' in task) || (oneOf(task.sample, realSamples) && isPreparation(task.preparation))))) &&
    oneOf(task.status, statuses) && typeof task.progress === 'number' && Number.isFinite(task.progress) &&
    task.progress >= 0 && task.progress <= 100 && optionalText(task.currentStep) && optionalText(task.error) &&
    date(task.createdAt) && date(task.updatedAt) && optionalDate(task.startedAt) && optionalDate(task.finishedAt) &&
    Array.isArray(task.logs) && task.logs.length <= 200 && task.logs.every((log: unknown) => record(log) &&
      only(log, ['id', 'time', 'level', 'message']) && text(log.id) && date(log.time) && text(log.message) &&
      oneOf(log.level, ['info', 'success', 'warning', 'error'])))
  const outputsValid = value.outputs.every((output: unknown) => record(output) &&
    only(output, ['id', 'taskId', 'title', 'status', 'createdAt', ...(output.source === 'real' ? ['source', 'deliveryPath'] : ['durationLabel', 'resolution', 'mock'])]) &&
    text(output.id) && text(output.taskId) && text(output.title) && oneOf(output.status, ['completed', 'quality_issue']) &&
    date(output.createdAt) && (output.source === 'real' ? text(output.deliveryPath) && Boolean(output.deliveryPath.trim()) :
      text(output.durationLabel) && output.resolution === '1920 × 1080' && output.mock === true))
  return tasksValid && outputsValid
}

function isPreparation(value: unknown): boolean {
  if (!record(value) || !only(value, ['status', 'taskRoot', 'pid', 'stdout', 'stdoutTruncated', 'stderr', 'stderrTruncated', 'processExit'])) return false
  const fact = value.processExit
  return oneOf(value.status, ['preparing', 'ready', 'failed', 'stopped']) && text(value.taskRoot) && text(value.stdout) && text(value.stderr) &&
    (!('stdoutTruncated' in value) || typeof value.stdoutTruncated === 'boolean') &&
    (!('stderrTruncated' in value) || typeof value.stderrTruncated === 'boolean') &&
    (value.pid === null || (typeof value.pid === 'number' && Number.isInteger(value.pid) && value.pid > 0)) &&
    (fact === null || (record(fact) && only(fact, ['code', 'signal', 'error']) &&
      (fact.code === null || Number.isInteger(fact.code)) && optionalText(fact.signal) && optionalText(fact.error)))
}
