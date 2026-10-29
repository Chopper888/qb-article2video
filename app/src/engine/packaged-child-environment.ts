export interface PrivateToolPaths {
  pythonRoot: string
  nodeRoot: string
  ffmpegBin: string
}

// In-memory spawn environment only. Never store this object in task state or diagnostics.
export function buildPackagedChildEnvironment(paths: PrivateToolPaths, parent: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env = { ...parent }
  const pathKey = Object.keys(parent).find(key => key.toUpperCase() === 'PATH')
  const originalPath = pathKey ? parent[pathKey] : undefined
  const overrides = new Set(['PATH', 'PYTHONDONTWRITEBYTECODE', 'PYTHONNOUSERSITE', 'NPM_CONFIG_UPDATE_NOTIFIER',
    'PYTHONIOENCODING', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP'])
  for (const key of Object.keys(env)) if (overrides.has(key.toUpperCase())) delete env[key]
  env.PATH = [paths.pythonRoot, paths.nodeRoot, paths.ffmpegBin, ...(originalPath ? [originalPath] : [])].join(';')
  env.PYTHONDONTWRITEBYTECODE = '1'
  env.PYTHONNOUSERSITE = '1'
  env.NPM_CONFIG_UPDATE_NOTIFIER = 'false'
  env.PYTHONIOENCODING = 'utf-8'
  return env
}
