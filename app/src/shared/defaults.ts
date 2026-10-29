import type { AppState } from './types'

export function createDefaultState(): AppState {
  return {
    schemaVersion: 1,
    tasks: [],
    outputs: [],
    settings: {
      theme: 'light',
      defaultPage: 'dashboard',
      outputView: 'list',
      openDetailAfterCreate: true,
      directories: {
        workspace: '',
        output: '',
        integration: '',
        logs: ''
      }
    },
    lastError: null
  }
}
