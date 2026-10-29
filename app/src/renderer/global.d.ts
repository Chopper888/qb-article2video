import type { AppApi } from '../shared/types'
import type { V1Api } from '../shared/v1/types'

declare global {
  interface Window {
    appApi: AppApi
    a2v: V1Api
  }
}

export {}
