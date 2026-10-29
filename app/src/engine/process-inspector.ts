import { resolveWindowsSystemPaths } from './windows-system-paths'

export interface ProcessQueryResult {
  code: number | null
  stdout: string
  stderr: string
}

export type ProcessQueryRunner = (
  executable: string,
  args: string[],
  options: { shell: false; windowsHide: true; timeout: number }
) => Promise<ProcessQueryResult>

export interface ProcessIdentityExpectation {
  requiredSequences: string[][]
}

export type ProcessInspection =
  | { outcome: 'not_running' }
  | { outcome: 'match'; fact: { processId: number; name: string | null; commandLine: string } }
  | { outcome: 'mismatch'; fact: { processId: number; name: string | null; commandLine: string } }
  | { outcome: 'unknown'; detail: string }

export interface ProcessInspector {
  inspect(pid: number, expected: ProcessIdentityExpectation): Promise<ProcessInspection>
}

export class WindowsProcessInspector implements ProcessInspector {
  constructor(private readonly run: ProcessQueryRunner = runProcessQuery,
    private readonly environment: NodeJS.ProcessEnv = process.env) {}

  async inspect(pid: number, expected: ProcessIdentityExpectation): Promise<ProcessInspection> {
    if (!Number.isInteger(pid) || pid <= 0) throw new Error('进程查询 PID 必须是正整数')
    if (!expected.requiredSequences.length || expected.requiredSequences.some(sequence => !sequence.length)) {
      return { outcome: 'unknown', detail: '缺少可靠的进程身份核对条件' }
    }

    let result: ProcessQueryResult
    try {
      result = await this.run(resolveWindowsSystemPaths(this.environment).powershell, buildEncodedPowerShellArgs(buildProcessQueryScript(pid)), { shell: false, windowsHide: true, timeout: PROCESS_QUERY_TIMEOUT_MS })
    } catch (error) {
      return { outcome: 'unknown', detail: `进程身份查询启动失败：${message(error)}` }
    }
    if (result.code === null) {
      return { outcome: 'unknown', detail: '进程身份查询超时或未正常完成' }
    }
    if (result.code !== 0) {
      return { outcome: 'unknown', detail: `进程身份查询失败：${result.stderr.trim() || result.stdout.trim() || `exit ${result.code}`}` }
    }

    let payload: unknown
    try { payload = JSON.parse(result.stdout.trim()) } catch {
      return { outcome: 'unknown', detail: '进程身份查询返回了无法解析的结果' }
    }
    if (!record(payload)) return { outcome: 'unknown', detail: '进程身份查询结果结构无效' }
    if (payload.status === 'not_running') return { outcome: 'not_running' }
    if (payload.status !== 'found' || payload.processId !== pid || !(payload.name === null || typeof payload.name === 'string')) {
      return { outcome: 'unknown', detail: '进程身份查询结果与目标 PID 不一致' }
    }
    if (typeof payload.commandLine !== 'string' || !payload.commandLine.trim()) {
      return { outcome: 'unknown', detail: '无法读取原进程 CommandLine' }
    }

    const fact = { processId: pid, name: payload.name, commandLine: payload.commandLine }
    const tokens = tokenize(payload.commandLine)
    const matches = expected.requiredSequences.every(sequence => containsSequence(tokens, sequence))
    return matches ? { outcome: 'match', fact } : { outcome: 'mismatch', fact }
  }
}
import { spawn } from 'node:child_process'

const PROCESS_QUERY_TIMEOUT_MS = 5000

function buildProcessQueryScript(pid: number): string {
  return [
    "$ErrorActionPreference = 'Stop'",
    '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)',
    `$targetPid = ${pid}`,
    '$item = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $targetPid)',
    'if ($null -eq $item) { [Console]::Out.Write(\'{"status":"not_running"}\'); exit 0 }',
    '[pscustomobject]@{ status = \'found\'; processId = [int]$item.ProcessId; name = $item.Name; commandLine = $item.CommandLine } | ConvertTo-Json -Compress'
  ].join('; ')
}

export function buildEncodedPowerShellArgs(script: string): string[] {
  return ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')]
}


const runProcessQuery: ProcessQueryRunner = (executable, args, options) => new Promise((resolve, reject) => {
  const child = spawn(executable, args, { ...options, stdio: 'pipe' })
  let stdout = ''
  let stderr = ''
  child.stdout.setEncoding('utf8')
  child.stderr.setEncoding('utf8')
  child.stdout.on('data', (chunk: string) => { stdout += chunk })
  child.stderr.on('data', (chunk: string) => { stderr += chunk })
  child.once('error', reject)
  child.once('close', code => resolve({ code, stdout, stderr }))
})

function tokenize(commandLine: string): string[] {
  return (commandLine.match(/(?:[^\s"]+|"[^"]*")+/g) ?? []).map(normalizeToken)
}

function containsSequence(tokens: string[], expected: string[]): boolean {
  const sequence = expected.map(normalizeToken)
  return tokens.some((_token, start) => sequence.every((token, offset) => tokens[start + offset] === token))
}

function normalizeToken(value: string): string {
  const unquoted = value.length >= 2 && value.startsWith('"') && value.endsWith('"') ? value.slice(1, -1) : value
  return unquoted.replaceAll('/', '\\').toLocaleLowerCase('en-US')
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
