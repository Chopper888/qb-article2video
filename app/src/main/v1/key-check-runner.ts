import { spawn, type ChildProcessWithoutNullStreams, type SpawnOptionsWithoutStdio } from 'node:child_process'
import type { KeyCheckAdapter, KeyCheckOutcome } from '../../shared/v1/adapter'
import { KEY_ENV_VARS } from '../../shared/v1/keys'
import { asKnownEvent, type KeyCheckEvent } from '../../shared/v1/protocol'
import type { EngineLocation } from './engine-location'

// 真实实现：python -m pipeline.engine check-keys（约定 1.4 节，检测不产生费用）。
// 密钥只出现在这一个子进程的环境变量里；stdout 只取 key_check / run_done 的状态字段，stderr 丢弃。

export type SpawnFn = (command: string, args: string[], options: SpawnOptionsWithoutStdio) => ChildProcessWithoutNullStreams

const SECRET_VARS = Object.values(KEY_ENV_VARS)
const TIMEOUT_MS = 120_000

// 父进程环境里可能本来就有同名变量（开发者终端），一律去掉，只用 APP 存的
export function buildKeyCheckEnv(parent: NodeJS.ProcessEnv, secrets: Record<string, string>): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  for (const [key, value] of Object.entries(parent)) if (!SECRET_VARS.includes(key.toUpperCase())) env[key] = value
  env.PYTHONDONTWRITEBYTECODE = '1'
  env.PYTHONIOENCODING = 'utf-8'
  for (const name of SECRET_VARS) if (secrets[name]) env[name] = secrets[name]
  return env
}

export class EngineKeyCheck implements KeyCheckAdapter {
  readonly kind = 'real' as const

  constructor(
    private readonly location: () => EngineLocation,
    private readonly secrets: () => Record<string, string>,
    private readonly log: (line: string) => void,
    private readonly spawnFn: SpawnFn = spawn,
    private readonly parentEnv: NodeJS.ProcessEnv = process.env
  ) {}

  checkKeys(services?: string[]): Promise<KeyCheckOutcome> {
    const checkedAt = new Date().toISOString()
    const { root, python } = this.location()
    if (!root || !python) return Promise.resolve({ ok: false, checkedAt, results: [], error: '找不到引擎，没法检测。请到"关于与诊断"看运行环境。' })
    const args = ['-m', 'pipeline.engine', 'check-keys', ...(services?.length ? ['--services', services.join(',')] : [])]
    return new Promise(resolve => {
      let child: ChildProcessWithoutNullStreams
      try {
        child = this.spawnFn(python, args, { cwd: root, env: buildKeyCheckEnv(this.parentEnv, this.secrets()), shell: false, windowsHide: true, stdio: 'pipe' })
      } catch {
        this.log('check-keys: 引擎进程没有启动')
        resolve({ ok: false, checkedAt, results: [], error: '引擎没有启动成功，没法检测。' })
        return
      }
      const results: KeyCheckEvent[] = []
      let exitCode: number | null = null
      let buffer = ''
      let timedOut = false
      const timer = setTimeout(() => { timedOut = true; child.kill() }, TIMEOUT_MS)
      const take = (line: string): void => {
        if (!line.trim()) return
        let parsed: unknown
        try { parsed = JSON.parse(line) } catch { return }
        const event = asKnownEvent(parsed)
        if (event?.event === 'key_check') results.push(pickKeyCheck(event))
        if (event?.event === 'run_done') exitCode = event.exit_code
      }
      child.stdout.setEncoding('utf8')
      child.stdout.on('data', (chunk: string) => {
        buffer += chunk
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        lines.forEach(take)
      })
      child.stderr.on('data', () => undefined)
      child.on('error', () => undefined)
      child.once('close', (code: number | null) => {
        clearTimeout(timer)
        take(buffer)
        const ok = !timedOut && code === 0 && exitCode === 0
        this.log(`check-keys: 退出码 ${code ?? '无'}${timedOut ? '（超时）' : ''}；${results.map(r => `${r.service}=${r.status}`).join(' ')}`)
        resolve({ ok, checkedAt, results, error: ok ? null : timedOut ? '检测超时，请稍后再试。' : '检测没有做完，请稍后再试。' })
      })
      child.stdin.end()
    })
  }
}

// 只保留界面要用的字段（余额只在内存里、只发给页面显示，不存盘）
function pickKeyCheck(event: KeyCheckEvent): KeyCheckEvent {
  return {
    schema_version: event.schema_version, seq: event.seq, time: event.time, event: 'key_check', step: null, message: '',
    service: event.service, service_name: event.service_name, status: event.status, env_vars: event.env_vars,
    ...(event.missing_keys ? { missing_keys: event.missing_keys } : {}),
    ...(Array.isArray(event.balance) ? { balance: event.balance.map(b => ({ currency: b.currency ?? null, total: b.total ?? null })) } : {})
  }
}
