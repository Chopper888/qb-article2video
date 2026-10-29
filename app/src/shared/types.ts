export type TaskStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'completed'
  | 'quality_issue'
  | 'failed'
  | 'stopped'
  | 'interrupted'

export type MockScenario = 'success' | 'quality_issue' | 'failure'
export type ThemeMode = 'system' | 'light' | 'dark'
export type OutputView = 'list' | 'grid'
export type AppPage = 'dashboard' | 'create' | 'tasks' | 'outputs' | 'settings'

export interface TaskLog {
  id: string
  time: string
  level: 'info' | 'success' | 'warning' | 'error'
  message: string
}

export type TaskSource = 'mock' | 'real'

interface TaskBase {
  id: string
  title: string
  status: TaskStatus
  progress: number
  currentStep: string | null
  createdAt: string
  updatedAt: string
  startedAt: string | null
  finishedAt: string | null
  error: string | null
  logs: TaskLog[]
}

export interface MockTask extends TaskBase {
  source: 'mock'
  scenario: MockScenario
}

// These are APP observation records, not additions to the engine protocol.
export interface RealStep {
  step: string
  name: string | null
  status: 'running' | 'done' | 'skipped' | 'failed'
  message: string
  outputs: string[]
}

export interface RealDiagnostic {
  kind: string
  message: string
  raw: string | null
}

export interface ProcessExitFact {
  code: number | null
  signal: string | null
  error: string | null
}

export interface RealRunData {
  observationTimeSource: 'app'
  // APP process association, not engine protocol fields. Optional additions preserve R2A JSON.
  association: {
    workRoot: string | null
    article: string | null
    processInstanceId: string | null
    pid?: number | null
    engineRoot?: string
    engineCommit?: string
    command?: 'run' | 'plan' | 'status'
  }
  steps: RealStep[]
  events: Record<string, unknown>[]
  diagnostics: RealDiagnostic[]
  stderr: string
  stderrTruncated?: boolean
  runDone: { exitCode: number; message: string; reason?: string } | null
  processExit: ProcessExitFact | null
  missingRunDone: boolean | null
  deliveryPath: string | null
  stepFailure: string | null
}

export interface RealTask extends TaskBase {
  source: 'real'
  real: RealRunData
  interruptionReason?: 'missing_pid' | 'process_missing' | 'process_mismatch' | 'process_identity_unknown'
  sample?: 'sample-v3.2' | 'sample-v4.1'
  preparation?: SamplePreparation
}

// APP prerequisite observation, never an engine step or an engine exit.
export interface SamplePreparation {
  status: 'preparing' | 'ready' | 'failed' | 'stopped'
  taskRoot: string
  pid: number | null
  stdout: string
  stdoutTruncated?: boolean
  stderr: string
  stderrTruncated?: boolean
  processExit: ProcessExitFact | null
}

export type Task = MockTask | RealTask

export interface MockOutput {
  id: string
  taskId: string
  title: string
  status: 'completed' | 'quality_issue'
  createdAt: string
  durationLabel: string
  resolution: '1920 × 1080'
  mock: true
}

export interface RealOutput {
  id: string
  taskId: string
  title: string
  source: 'real'
  status: 'completed' | 'quality_issue'
  createdAt: string
  deliveryPath: string
}

export type TaskOutput = MockOutput | RealOutput

export interface DirectorySettings {
  workspace: string
  output: string
  integration: string
  logs: string
}

export interface AppSettings {
  theme: ThemeMode
  defaultPage: AppPage
  outputView: OutputView
  openDetailAfterCreate: boolean
  directories: DirectorySettings
}

export interface AppState {
  schemaVersion: 1
  tasks: Task[]
  outputs: TaskOutput[]
  settings: AppSettings
  lastError: string | null
}

export interface CreateTaskInput {
  title: string
  scenario: MockScenario
}

export interface EngineEvent {
  taskId: string
  // Internal persistence scheduling hint. Never an engine protocol fact.
  diagnosticsOnly?: boolean
  status?: TaskStatus
  progress?: number
  currentStep?: string | null
  log?: Omit<TaskLog, 'id' | 'time'>
  error?: string | null
  real?: RealRunData
  preparation?: SamplePreparation
}

export interface StateStorage {
  load(): Promise<AppState>
  save(state: AppState): Promise<void>
}

export interface Diagnostics {
  appVersion: string
  mode: 'Mock + Real'
  electronVersion: string
  nodeVersion: string
  userDataPath: string
  theme: ThemeMode
  taskCount: number
  runningTask: string | null
  lastError: string | null
}

export interface EnvironmentCheck {
  id: string
  status: 'ready' | 'blocked'
  message: string
  path?: string
}

// A read-only snapshot, independent of persisted task/engine schemas.
export interface EnvironmentReadiness {
  status: 'ready' | 'blocked'
  checks: EnvironmentCheck[]
}

export interface AppApi {
  getEnvironmentReadiness(): Promise<EnvironmentReadiness>
  getState(): Promise<AppState>
  createTask(input: CreateTaskInput): Promise<MockTask>
  createSampleTask(input: { sample: 'sample-v4.1' }): Promise<RealTask>
  startTask(taskId: string): Promise<void>
  stopTask(taskId: string): Promise<void>
  retryTask(taskId: string): Promise<void>
  updateSettings(patch: Partial<AppSettings>): Promise<AppSettings>
  getDiagnostics(): Promise<Diagnostics>
  onStateChanged(listener: (state: AppState) => void): () => void
}
