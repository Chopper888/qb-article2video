import { constants } from 'node:fs'
import { access, lstat, realpath } from 'node:fs/promises'
import { win32 } from 'node:path'
import baseline from '../shared/runtime-baseline.json'
import type { SampleResources } from '../engine/engine-resources'

export interface RuntimePreflight { check(): Promise<string> }
export interface RuntimeFileSystem {
  lstat(path: string): Promise<{ isDirectory(): boolean; isSymbolicLink(): boolean }>
  realpath(path: string): Promise<string>
  access(path: string, mode: number): Promise<void>
}

export function resolveRuntimeDirectory(environment: NodeJS.ProcessEnv = process.env): string {
  const local = environment.LOCALAPPDATA
  // Reject drive-relative, UNC, device namespace and root-only locations.
  if (!local || !/^[a-z]:[\\/]/i.test(local) || /[\0\r\n]/.test(local) ||
    win32.normalize(local) === win32.parse(local).root) throw new Error('用户级 LOCALAPPDATA 未配置为绝对目录')
  return win32.join(win32.resolve(local), 'wzcp', `rt-${baseline.version}`)
}

export function runtimePathBudget(runtimeDir: string) {
  if (!win32.isAbsolute(runtimeDir)) throw new Error('Runtime 工作路径必须为绝对路径')
  // The assembler/verifier checks this frozen relative path against every actual Runtime entry.
  // JS length counts UTF-16 code units, matching the conservative Windows visible-path budget.
  const finalLength = win32.join(runtimeDir, baseline.longestRelativePath).length
  const temporaryLength = win32.join(`${runtimeDir}.tmp`, baseline.longestRelativePath).length
  if (Math.max(finalLength, temporaryLength) > baseline.maxTargetCharacters) {
    throw new Error(`Runtime 工作路径过长：最终 ${finalLength} / 临时 ${temporaryLength}，上限 ${baseline.maxTargetCharacters} 字符`)
  }
  return { finalLength, temporaryLength, limit: baseline.maxTargetCharacters }
}

// Read-only, fail closed. No rm API: unknown legacy process liveness cannot justify deleting a residual.
export class RuntimeWorkspace implements RuntimePreflight {
  private readonly environment: NodeJS.ProcessEnv
  constructor(private readonly resources: SampleResources | null, environment: NodeJS.ProcessEnv = process.env,
    private readonly fs: RuntimeFileSystem = { lstat, realpath, access }) {
    this.environment = { LOCALAPPDATA: environment.LOCALAPPDATA }
  }

  async check(): Promise<string> {
    const runtimeDir = resolveRuntimeDirectory(this.environment)
    runtimePathBudget(runtimeDir)
    if (this.resources?.engineCommit !== baseline.engineCommit) throw new Error('短 Runtime 仅供冻结的 v5 引擎使用')
    const local = win32.dirname(win32.dirname(runtimeDir))
    const controlled = win32.dirname(runtimeDir)
    const source = win32.resolve(this.resources.runtimeSource).toLowerCase()
    if (runtimeDir.toLowerCase() === source || runtimeDir.toLowerCase().startsWith(source + '\\')) {
      throw new Error('Runtime 工作路径不能位于发布源内')
    }
    // lstat catches even dangling links at the exact residual name; access errors also block.
    try {
      await this.fs.lstat(`${runtimeDir}.tmp`)
      throw new Error(`发现 Runtime 准备残留：${runtimeDir}.tmp；无法证明无遗留进程，已阻止启动，未自动删除`)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
    for (const path of [local, controlled, runtimeDir]) {
      try {
        const info = await this.fs.lstat(path)
        if (!info.isDirectory() || info.isSymbolicLink() ||
          win32.normalize(await this.fs.realpath(path)).toLowerCase() !== win32.normalize(path).toLowerCase()) {
          throw new Error('Runtime 工作路径存在文件占用或目录重定向')
        }
        await this.fs.access(path, constants.W_OK)
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT' || path === local) throw error
      }
    }
    return runtimeDir
  }
}
