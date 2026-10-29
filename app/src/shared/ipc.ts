export const IPC = {
  getEnvironmentReadiness: 'app:get-environment-readiness',
  getState: 'app:get-state',
  createTask: 'task:create',
  createSampleTask: 'task:create-sample',
  startTask: 'task:start',
  stopTask: 'task:stop',
  retryTask: 'task:retry',
  updateSettings: 'settings:update',
  getDiagnostics: 'app:get-diagnostics',
  stateChanged: 'app:state-changed'
} as const
