import { win32 } from 'node:path'
import type { SampleResources } from '../engine/engine-resources'
import { buildPackagedChildEnvironment } from '../engine/packaged-child-environment'
import type { AppPaths } from './app-paths'
import baseline from '../shared/runtime-baseline.json'

export const PACKAGED_ENGINE_COMMIT = baseline.engineCommit
export const PACKAGED_WHITELIST_SHA = baseline.whitelistSha256
export interface PackagedResources extends SampleResources {
  runtimePackRoot: string
  pythonRoot: string
  nodeRoot: string
  ffmpegBin: string
  manifestPath: string
}

// Pure Windows layout resolution: no cwd, local configuration, filesystem or process.env.
export function resolvePackagedResources(paths: Pick<AppPaths, 'isPackaged' | 'resourcesRoot'>): PackagedResources | null {
  if (!paths.isPackaged || !win32.isAbsolute(paths.resourcesRoot) || /[\0\r\n]/.test(paths.resourcesRoot)) return null
  const root = win32.join(paths.resourcesRoot, `runtime-pack-${baseline.version}`)
  const tools = { pythonRoot: win32.join(root, 'python'), nodeRoot: win32.join(root, 'node'), ffmpegBin: win32.join(root, 'ffmpeg', 'bin') }
  return {
    runtimePackRoot: root, ...tools,
    engineRoot: win32.join(root, 'engine'), engineCommit: PACKAGED_ENGINE_COMMIT,
    pythonExecutable: win32.join(tools.pythonRoot, 'python.exe'),
    runtimeSource: win32.join(root, 'runtime'), sampleSourceRoot: win32.join(root, 'samples'),
    manifestPath: win32.join(root, 'manifest', 'runtime-pack.json'),
    childEnvironment: parent => buildPackagedChildEnvironment(tools, parent)
  }
}

export function manifestObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('无效 Runtime manifest')
  return value as Record<string, unknown>
}

// Validate only existing stable R7c fields; buildSources is diagnostic and never a resolver input.
export function validatePackagedManifest(value: unknown): Record<string, unknown> {
  const m = manifestObject(value)
  if (m.schemaVersion !== 1 || m.article2videoVersion !== baseline.version || m.engineCommit !== PACKAGED_ENGINE_COMMIT ||
    m.revision !== 'S4-R7c' || m.localVerificationPassed !== true) throw new Error('Runtime 基线不符')
  const budget = manifestObject(m.runtimePathBudget)
  if (budget.longestRelativePath !== baseline.longestRelativePath || budget.maxTargetCharacters !== baseline.maxTargetCharacters) throw new Error('Runtime 路径预算基线不符')
  const assets = manifestObject(m.assets)
  for (const [name, version] of Object.entries({ python: '3.12.10', node: '24.19.0', ffmpeg: 'n9.0.2-25-g67b60c310b' })) {
    if (manifestObject(assets[name]).version !== version) throw new Error('Runtime 工具版本不符')
  }
  const assembly = manifestObject(m.assembly)
  for (const name of ['engine', 'runtime', 'samples', 'python', 'node', 'ffmpeg', 'fontLicense']) {
    if (manifestObject(assembly[name]).status !== 'PASS') throw new Error('Runtime 组装未通过')
  }
  if (manifestObject(m.enginePublishWhitelist).sha256 !== PACKAGED_WHITELIST_SHA) throw new Error('Engine 白名单不符')
  return m
}
