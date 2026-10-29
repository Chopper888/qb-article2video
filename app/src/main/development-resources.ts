import { readFileSync } from 'node:fs'
import { isAbsolute, join } from 'node:path'
import type { SampleResources } from '../engine/engine-resources'
import type { AppPaths } from './app-paths'

// Machine-specific values stay outside the bundle. Never inspect this file in packaged mode.
export function resolveDevelopmentResources(paths: Pick<AppPaths, 'isPackaged' | 'resourcesRoot'>,
  readConfig: (file: string) => string = file => readFileSync(file, 'utf8')): SampleResources | null {
  if (paths.isPackaged) return null
  try {
    if (!isAbsolute(paths.resourcesRoot)) return null
    const value: unknown = JSON.parse(readConfig(join(paths.resourcesRoot, '.cache', 'development-resources.local.json')))
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null
    const config = value as Record<string, unknown>
    const keys = ['engineRoot', 'engineCommit', 'pythonExecutable', 'runtimeSource', 'sampleSourceRoot'] as const
    for (const key of keys) {
      const field = config[key]
      if (typeof field !== 'string' || !field.trim() || /[\0\r\n]/.test(field)) return null
      if (key === 'engineCommit' ? !/^[a-f0-9]{40}$/i.test(field) : !isAbsolute(field)) return null
    }
    return Object.fromEntries(keys.map(key => [key, config[key]])) as unknown as SampleResources
  } catch {
    // Missing, unreadable or invalid local config blocks Real through existing readiness.
    return null
  }
}
