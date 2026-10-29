import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

export interface AppPaths {
  isPackaged: boolean
  usesLocalUserData: boolean
  resourcesRoot: string
  userDataRoot: string
  realTaskRoot: string
}

interface PathInputs {
  isPackaged: boolean
  resourcesPath: string
  developmentRoot: string
  userData: string
  cwd: string
  useLocalUserData: boolean
}

// Pure path policy: no Electron, filesystem writes or engine resource layout assumptions.
export function resolveAppPaths(input: PathInputs): AppPaths {
  const usesLocalUserData = !input.isPackaged && input.useLocalUserData
  const userDataRoot = usesLocalUserData
    ? join(input.cwd, '.cache', 'smoke-user-data') : input.userData
  return {
    isPackaged: input.isPackaged,
    usesLocalUserData,
    resourcesRoot: input.isPackaged ? input.resourcesPath : input.developmentRoot,
    userDataRoot,
    realTaskRoot: join(userDataRoot, 'real-tasks')
  }
}

interface PathApp {
  isPackaged: boolean
  getPath(name: 'userData'): string
  getAppPath(): string
  setPath(name: 'userData', path: string): void
}

interface PathRuntime {
  resourcesPath: string
  cwd(): string
  env: NodeJS.ProcessEnv
}

// Call before app.ready so Electron and APP storage share the same userData.
// Only this boundary reads Electron/process path facts.
export function initializeAppPaths(app: PathApp, runtime: PathRuntime = process,
  ensureDirectory: (path: string) => void = path => { mkdirSync(path, { recursive: true }) }): AppPaths {
  const paths = resolveAppPaths({
    isPackaged: app.isPackaged,
    resourcesPath: app.isPackaged ? runtime.resourcesPath : '',
    developmentRoot: app.isPackaged ? '' : app.getAppPath(),
    userData: app.getPath('userData'),
    cwd: app.isPackaged ? '' : runtime.cwd(),
    useLocalUserData: !app.isPackaged && runtime.env.WZCP_USE_LOCAL_USER_DATA === '1'
  })
  if (paths.usesLocalUserData) {
    ensureDirectory(paths.userDataRoot)
    app.setPath('userData', paths.userDataRoot)
  }
  return paths
}
