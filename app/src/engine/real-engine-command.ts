import { win32 } from 'node:path'

import type { EngineResources } from './engine-resources'

export const BUSINESS_KEY_NAMES = ['ARK_API_KEY', 'DEEPSEEK_API_KEY', 'MODEL_SPEECH_API_KEY', 'RUNNINGHUB_API_KEY'] as const

export interface EngineRequest {
  command: 'run' | 'plan' | 'status'
  workRoot: string
  article: string
  deliveryRoot?: string
  tmpDir?: string
  runtimeDir?: string
}

export interface ProcessCommand {
  executable: string
  args: string[]
  cwd: string
  env: NodeJS.ProcessEnv
  // APP2：Mac/Linux 上让引擎自成进程组，停止时整组发信号（Windows 用 taskkill /T，不用这一项）
  detached?: boolean
}

export function buildPythonEnvironment(parent: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const excluded = new Set<string>([...BUSINESS_KEY_NAMES, 'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'A2V_MAX_COST_CNY', 'PYTHONDONTWRITEBYTECODE', 'PYTHONIOENCODING'])
  const env: NodeJS.ProcessEnv = {}
  for (const key of Object.keys(parent)) {
    // Filter names before accessing values. Windows environment names are case-insensitive.
    if (excluded.has(key.toUpperCase())) continue
    const value = parent[key]
    if (value !== undefined) env[key] = value
  }
  env.PYTHONDONTWRITEBYTECODE = '1'
  env.PYTHONIOENCODING = 'utf-8'
  return env
}

function absolutePath(value: string, field: string): string {
  if (!win32.isAbsolute(value) || /[\0\r\n]/.test(value)) throw new Error(`${field} 必须是绝对 Windows 路径`)
  return value
}

export function buildEngineEnvironment(resources: EngineResources, parent: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return resources.childEnvironment ? resources.childEnvironment(parent) : buildPythonEnvironment(parent)
}

// This boundary constructs argv only; constructing run is not permission to execute it.
export function buildEngineCommand(request: EngineRequest, resources: EngineResources, environment: NodeJS.ProcessEnv = process.env): ProcessCommand {
  if (!['run', 'plan', 'status'].includes(request.command)) throw new Error('不支持的 command')
  if (!request.article || request.article.startsWith('-') || /[\\/\0\r\n:]/.test(request.article) || ['.', '..'].includes(request.article)) throw new Error('article 必须是单个样本目录名')
  if (request.command === 'run' && (!request.deliveryRoot || !request.tmpDir || !request.runtimeDir)) throw new Error('run 必须显式提供 D/T/R')
  const args = ['-B', '-u', '-m', 'pipeline.engine', request.command, '--work-root', absolutePath(request.workRoot, 'workRoot'), '--article', request.article]
  for (const [field, flag] of [['deliveryRoot', '--delivery-root'], ['tmpDir', '--tmp-dir'], ['runtimeDir', '--runtime-dir']] as const) {
    const value = request[field]
    if (value !== undefined) args.push(flag, absolutePath(value, field))
  }
  if (request.command === 'run') args.push('--no-paid')
  return { executable: resources.pythonExecutable, args, cwd: resources.engineRoot, env: buildEngineEnvironment(resources, environment) }
}
