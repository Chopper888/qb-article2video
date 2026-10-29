import { spawn } from 'node:child_process'
import { resolveWindowsSystemPaths } from './windows-system-paths'

export interface TaskkillCommandResult {
  code: number | null
  stdout: string
  stderr: string
}

export type TaskkillRunner = (
  executable: string,
  args: string[],
  options: { shell: false; windowsHide: true }
) => Promise<TaskkillCommandResult>

export interface ProcessTreeTerminationResult {
  outcome: 'terminated' | 'target_missing'
  taskkillExitCode: number | null
}

export interface ProcessTreeTerminator {
  terminate(pid: number): Promise<ProcessTreeTerminationResult>
}

export class TaskkillProcessTreeTerminator implements ProcessTreeTerminator {
  constructor(
    private readonly run: TaskkillRunner = runTaskkill,
    private readonly isAlive: (pid: number) => boolean = isProcessAlive,
    private readonly mainProcessPid = process.pid,
    private readonly environment: NodeJS.ProcessEnv = process.env
  ) {}

  async terminate(pid: number): Promise<ProcessTreeTerminationResult> {
    if (!Number.isInteger(pid) || pid <= 0) throw new Error('停止目标 PID 必须是正整数')
    if (pid === this.mainProcessPid) throw new Error('拒绝终止 Electron 主进程')

    let result: TaskkillCommandResult
    try {
      result = await this.run(resolveWindowsSystemPaths(this.environment).taskkill, ['/PID', String(pid), '/T', '/F'], { shell: false, windowsHide: true })
    } catch (error) {
      throw new Error(`taskkill 启动失败：${error instanceof Error ? error.message : String(error)}`)
    }
    if (result.code === 0) return { outcome: 'terminated', taskkillExitCode: result.code }
    if (!this.isAlive(pid)) return { outcome: 'target_missing', taskkillExitCode: result.code }

    const detail = result.stderr.trim() || result.stdout.trim() || `exit ${result.code}`
    throw new Error(`taskkill 失败：${detail}`)
  }
}

const runTaskkill: TaskkillRunner = (executable, args, options) => new Promise((resolve, reject) => {
  const child = spawn(executable, args, { ...options, stdio: 'pipe' })
  let stdout = ''
  let stderr = ''
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', (chunk: string) => { stdout += chunk })
  child.stderr.on('data', (chunk: string) => { stderr += chunk })
  child.once('error', reject)
  child.once('close', (code) => resolve({ code, stdout, stderr }))
})

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== 'ESRCH'
  }
}
