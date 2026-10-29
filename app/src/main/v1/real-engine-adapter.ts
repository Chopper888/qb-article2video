import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, posix, win32 } from 'node:path'
import type { ArticleEngineAdapter, ArticleRequest, EngineSession, EventSink, ImportRequest, RunRequest } from '../../shared/v1/adapter'
import { KEY_ENV_VARS } from '../../shared/v1/keys'
import { asKnownEvent, type CheckReport, type EngineEvent, type RunDoneEvent } from '../../shared/v1/protocol'
import type { ProcessCommand } from '../../engine/real-engine-command'
import type { ProcessTransport } from '../../engine/process-transport'
import type { ProcessTreeTerminator } from '../../engine/process-tree-terminator'
import type { EngineLocation } from './engine-location'

// 真实实现（任务书 APP2 3.1）：每个操作启动一次 python -m pipeline.engine <子命令>。
// 子进程启动复用旧主进程的 NodeProcessTransport；强制停止复用旧的进程树终止（Windows taskkill /T /F）。
// 密钥只在启动时放进这一个子进程的环境变量；不写日志、不进事件记录。
// APP2b 温和停止（约定 v5.4）：先写停止标记 <工作文件夹>/<文章>/.stop-request（Mac / Linux 另发 SIGINT），
// 等引擎自己收尾（退出码 3 的 run_done）最多 30 秒，超时才强制结束、补记退出码 3。

export type EngineOp = 'import' | 'estimate' | 'run' | 'resume'

export interface RealEngineOptions {
  location: () => EngineLocation
  secrets: () => Record<string, string>
  deliveryRoot: () => string
  // 开发者开关"不付费试跑"：打开时 run / resume 带 --no-paid（只在开发模式出现）
  noPaid: () => boolean
  transport: ProcessTransport
  terminator: ProcessTreeTerminator
  log: (line: string) => void
  platform?: NodeJS.Platform
  parentEnv?: NodeJS.ProcessEnv
  // 发信号给整个进程组（Mac/Linux）；测试替身用
  signalGroup?: (pid: number, signal: NodeJS.Signals) => void
  // 停止时先请引擎自己收尾，等这么久还没退出就强制结束（默认 30 秒）
  stopGraceMs?: number
  // 写 / 删停止标记文件；测试替身用
  writeStopMarker?: (file: string) => void
  removeStopMarker?: (file: string) => void
  // 每次运行结束时（含 APP 补的 run_done）回调，诊断包和通知用
  onFinished?: (info: FinishedRun) => void
  // APP4：出片期间阻止电脑睡眠（Electron powerSaveBlocker 'prevent-app-suspension'，显示器照常可以关）。
  // run / resume 开始时 start，结束、停止、失败（进程退出）时 stop
  keepAwake?: KeepAwake
}

export interface KeepAwake { start(): number | null; stop(id: number): void }

export interface FinishedRun { op: EngineOp; article: string | null; events: EngineEvent[]; runDone: RunDoneEvent }

const SECRET_VARS = Object.values(KEY_ENV_VARS)
// 启动引擎时不从 APP 自己的环境继承这些（密钥只用 APP 存的；出图服务切换只给开发者在命令行用）
const DROPPED = new Set([...SECRET_VARS, 'A2V_IMAGE_SERVICE', 'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONDONTWRITEBYTECODE',
  'PYTHONIOENCODING', 'A2V_TEMP_DIR', 'A2V_MAX_COST_CNY'])

export function buildRunEnvironment(parent: NodeJS.ProcessEnv, secrets: Record<string, string>,
  options: { workRoot: string; ffmpegDir: string | null; platform: NodeJS.Platform }): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  let pathKey = 'PATH'
  for (const [key, value] of Object.entries(parent)) {
    if (DROPPED.has(key.toUpperCase()) || value === undefined) continue
    if (key.toUpperCase() === 'PATH') pathKey = key
    env[key] = value
  }
  env.PYTHONDONTWRITEBYTECODE = '1'
  env.PYTHONIOENCODING = 'utf-8'
  env.A2V_TEMP_DIR = (options.platform === 'win32' ? winJoin : join)(options.workRoot, '_a2v_tmp')
  if (options.ffmpegDir) {
    const sep = options.platform === 'win32' ? ';' : ':'
    env[pathKey] = [options.ffmpegDir, env[pathKey]].filter(Boolean).join(sep)
  }
  for (const name of SECRET_VARS) if (secrets[name]) env[name] = secrets[name]
  return env
}

const winJoin = (a: string, b: string): string => `${a.replace(/[\\/]+$/, '')}\\${b}`

export const STOP_MARKER_NAME = '.stop-request'
export const STOP_GRACE_MS = 30_000

// 停止标记的位置（约定 v5.4）：文章文件夹里的 .stop-request
export function stopMarkerPath(workRoot: string, article: string, platform: NodeJS.Platform): string {
  return (platform === 'win32' ? win32 : posix).join(workRoot, checkArticleName(article), STOP_MARKER_NAME)
}

export function checkArticleName(article: string): string {
  if (!article || article.startsWith('-') || /[\\/\0\r\n:]/.test(article) || article === '.' || article === '..' || article.startsWith('_')) {
    throw new Error('文章名不对')
  }
  return article
}

export interface ArgsInput {
  op: EngineOp
  workRoot: string
  input?: string
  article?: string
  maxCostCny?: number
  deliveryRoot?: string
  runtimeDir?: string | null
  noPaid?: boolean
}

// 命令行（约定 v5.2 / v5.3 第 1 节）。续跑、补图和运行同一条命令，不带 --from（约定第 5 节）。
export function buildEngineArgs(input: ArgsInput): string[] {
  const base = ['-u', '-m', 'pipeline.engine']
  if (!isAbsolute(input.workRoot)) throw new Error('工作文件夹必须是绝对路径')
  if (input.op === 'import') {
    if (!input.input || /[\0\r\n]/.test(input.input) || input.input.startsWith('-')) throw new Error('文章链接或文件夹不对')
    return [...base, 'import', '--work-root', input.workRoot, '--input', input.input]
  }
  const article = checkArticleName(input.article ?? '')
  if (input.op === 'estimate') return [...base, 'estimate', '--work-root', input.workRoot, '--article', article]
  const cost = input.maxCostCny
  if (typeof cost !== 'number' || !Number.isFinite(cost) || cost <= 0) throw new Error('费用上限不对')
  if (!input.deliveryRoot || !isAbsolute(input.deliveryRoot)) throw new Error('成片保存位置必须是绝对路径')
  if (!input.runtimeDir || !isAbsolute(input.runtimeDir)) throw new Error('没有找到运行环境（--runtime-dir）')
  const args = [...base, 'run', '--work-root', input.workRoot, '--article', article, '--max-cost-cny', String(cost),
    '--delivery-root', input.deliveryRoot, '--runtime-dir', input.runtimeDir]
  if (input.noPaid) args.push('--no-paid')
  return args
}

// 导入输入：飞书链接、本地文件夹、.zip、单个 .md 都原样交给引擎（约定 v5.5 第 3 节）。
// APP4 起单个 .md 不再换成所在文件夹：引擎只用这一个 md（文件夹里别的 md 不算"多个 md"）。
export function normalizeImportInput(value: string): string {
  return value.trim()
}

interface Session {
  id: string
  op: EngineOp
  article: string | null
  pid: number | null
  workRoot: string
  stopRequested: boolean
  stopping: Promise<void> | null
  closed: boolean
  events: EngineEvent[]
  closedPromise: Promise<void>
  awakeId: number | null
}

export class RealEngineAdapter implements ArticleEngineAdapter {
  readonly kind = 'real' as const
  private counter = 0
  private readonly sessions = new Map<string, Session>()
  private activeRunId: string | null = null
  private readonly reportPaths = new Map<string, string>()
  private readonly platform: NodeJS.Platform
  private readonly parentEnv: NodeJS.ProcessEnv
  private readonly signalGroup: (pid: number, signal: NodeJS.Signals) => void

  constructor(private readonly options: RealEngineOptions) {
    this.platform = options.platform ?? process.platform
    this.parentEnv = options.parentEnv ?? process.env
    this.signalGroup = options.signalGroup ?? ((pid, signal) => process.kill(-pid, signal))
  }

  importArticle(request: ImportRequest, onEvent: EventSink): EngineSession {
    return this.start('import', null, request.workRoot, { op: 'import', workRoot: request.workRoot, input: request.input }, onEvent)
  }

  estimate(request: ArticleRequest, onEvent: EventSink): EngineSession {
    return this.start('estimate', request.article, request.workRoot, { op: 'estimate', workRoot: request.workRoot, article: request.article }, onEvent)
  }

  run(request: RunRequest, onEvent: EventSink): EngineSession { return this.startRun('run', request, onEvent) }

  resume(request: RunRequest, onEvent: EventSink): EngineSession { return this.startRun('resume', request, onEvent) }

  // 正在出片的那一次（关闭保护、任务列表"进行中"用）
  activeRun(): { sessionId: string; article: string | null } | null {
    const session = this.activeRunId ? this.sessions.get(this.activeRunId) : null
    return session && !session.closed ? { sessionId: session.id, article: session.article } : null
  }

  // 停止（按钮、关闭 APP 都走这里）：重复调用拿到同一个过程
  stop(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId)
    if (!session || session.closed) return Promise.resolve()
    session.stopping ??= this.stopSession(session)
    return session.stopping
  }

  private async stopSession(session: Session): Promise<void> {
    session.stopRequested = true
    const pid = session.pid
    if (!pid) return
    // ① 写停止标记：引擎在检查点发现后自己收尾（run_done 退出码 3、释放运行锁、删掉标记）
    const marker = (session.op === 'run' || session.op === 'resume') && session.article
      ? stopMarkerPath(session.workRoot, session.article, this.platform) : null
    if (marker) {
      try { (this.options.writeStopMarker ?? defaultWriteMarker)(marker) } catch (error) {
        this.options.log(`停止：写停止标记失败（${error instanceof Error ? error.message : String(error)}）`)
      }
    }
    // ② Mac / Linux 另外照旧给整个进程组发 SIGINT
    if (this.platform !== 'win32') {
      try { this.signalGroup(pid, 'SIGINT') } catch { /* 已经退出 */ }
    }
    // ③ 等引擎自己退出
    const grace = this.options.stopGraceMs ?? STOP_GRACE_MS
    let timer: ReturnType<typeof setTimeout> | undefined
    const timedOut = await Promise.race([session.closedPromise.then(() => false),
      new Promise<boolean>(resolve => { timer = setTimeout(() => resolve(true), grace) })])
    clearTimeout(timer)
    if (!timedOut || session.closed) return
    // ④ 超时：强制结束。引擎来不及发 run_done，由 start() 里补一条退出码 3（synthesized）
    this.options.log(`停止：引擎 ${grace / 1000} 秒内没有退出，强制结束`)
    if (this.platform === 'win32') {
      try { await this.options.terminator.terminate(pid) } catch (error) {
        this.options.log(`停止：强制结束失败（${error instanceof Error ? error.message : String(error)}）`)
      }
    } else {
      try { this.signalGroup(pid, 'SIGKILL') } catch { /* 已经退出 */ }
    }
    await session.closedPromise
    // 引擎没来得及删的标记由 APP 收掉（不收也无妨：引擎下次开跑会删掉旧标记）
    if (marker) {
      try { (this.options.removeStopMarker ?? defaultRemoveMarker)(marker) } catch { /* 忽略 */ }
    }
  }

  async readReport(request: ArticleRequest): Promise<CheckReport | null> {
    const article = checkArticleName(request.article)
    const file = this.reportPaths.get(`${request.workRoot}\0${article}`) ?? join(request.workRoot, article, '10-报告', '检查结果.json')
    try { return JSON.parse(readFileSync(file, 'utf8')) as CheckReport } catch { return null }
  }

  private startRun(op: 'run' | 'resume', request: RunRequest, onEvent: EventSink): EngineSession {
    const current = this.activeRun()
    if (current) throw new Error(`已经有一篇在出片（${current.article ?? '未知'}），等它完成或先停止`)
    const location = this.options.location()
    const args: ArgsInput = { op, workRoot: request.workRoot, article: request.article, maxCostCny: request.maxCostCny,
      deliveryRoot: this.options.deliveryRoot(), runtimeDir: location.runtimeDir ?? null, noPaid: this.options.noPaid() }
    const session = this.start(op, request.article, request.workRoot, args, onEvent)
    this.activeRunId = session.sessionId
    const record = this.sessions.get(session.sessionId)
    if (record && !record.closed && this.options.keepAwake) {
      try {
        record.awakeId = this.options.keepAwake.start()
        this.options.log('出片期间阻止电脑睡眠：已打开')
      } catch (error) { this.options.log(`阻止睡眠没打开（${error instanceof Error ? error.message : String(error)}）`) }
    }
    return session
  }

  private start(op: EngineOp, article: string | null, workRoot: string, argsInput: ArgsInput, onEvent: EventSink): EngineSession {
    const location = this.options.location()
    if (!location.root || !location.python) throw new Error('找不到引擎，请到"设置 → 关于与诊断"看运行环境')
    if (!existsSync(location.root)) throw new Error(`引擎目录不存在：${location.root}`)
    const args = buildEngineArgs(argsInput)
    const command: ProcessCommand = {
      executable: location.python, args, cwd: location.root,
      env: buildRunEnvironment(this.parentEnv, this.options.secrets(), { workRoot, ffmpegDir: location.ffmpegDir ?? null, platform: this.platform }),
      detached: this.platform !== 'win32'
    }
    const id = `real-${++this.counter}`
    let markClosed: () => void = () => undefined
    const session: Session = { id, op, article, workRoot, pid: null, stopRequested: false, stopping: null, closed: false, events: [], awakeId: null,
      closedPromise: new Promise<void>(resolve => { markClosed = resolve }) }
    this.sessions.set(id, session)
    let buffer = ''
    let runDone: RunDoneEvent | null = null
    const decoder = new TextDecoder('utf-8')
    const take = (line: string): void => {
      if (!line.trim()) return
      let parsed: unknown
      try { parsed = JSON.parse(line) } catch { return }
      const event = asKnownEvent(parsed)
      if (!event) return
      if (event.event === 'input_resolved') session.article = event.article
      if (event.event === 'run_done') {
        runDone = event
        if (event.article) session.article = event.article
        if (typeof event.report_json === 'string' && session.article) this.reportPaths.set(`${workRoot}\0${session.article}`, event.report_json)
      }
      session.events.push(event)
      onEvent(event)
    }
    this.options.log(`引擎 ${op} 开始${article ? `：${article}` : ''}${argsInput.noPaid ? '（不付费试跑）' : ''}`)
    const done = this.options.transport.execute(command, {
      spawned: pid => { session.pid = pid },
      stdout: chunk => {
        buffer += decoder.decode(chunk, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        lines.forEach(line => take(line.replace(/\r$/, '')))
      },
      // stderr 是引擎的运行日志（已经写进文章的 logs/），这里不收
      stderr: () => undefined
    }).then(fact => {
      buffer += decoder.decode()
      take(buffer.replace(/\r$/, ''))
      buffer = ''
      if (!runDone) {
        // 没有 run_done：被停止时补一条"已停止"（退出码 3），否则按引擎异常退出（退出码 2、internal）
        const stopped = session.stopRequested
        const lastSeq = session.events.at(-1)?.seq ?? 0
        const synthesized: RunDoneEvent = {
          schema_version: 1, seq: lastSeq + 1, time: new Date().toISOString(), event: 'run_done', step: null,
          message: stopped ? '已停止' : `引擎异常退出（退出码 ${fact.code ?? '无'}${fact.signal ? `，信号 ${fact.signal}` : ''}${fact.error ? `，${fact.error}` : ''}）`,
          exit_code: stopped ? 3 : 2, error_kind: stopped ? 'interrupted' : 'internal', service: null,
          ...(session.article ? { article: session.article } : {}), synthesized: true
        }
        runDone = synthesized
        session.events.push(synthesized)
        onEvent(synthesized)
      }
      const final = runDone as RunDoneEvent
      session.closed = true
      if (this.activeRunId === id) this.activeRunId = null
      if (session.awakeId !== null) {
        try { this.options.keepAwake?.stop(session.awakeId); this.options.log('出片期间阻止电脑睡眠：已关闭') } catch { /* 忽略 */ }
        session.awakeId = null
      }
      markClosed()
      this.options.log(`引擎 ${op} 结束：退出码 ${final.exit_code}${final.error_kind ? `（${final.error_kind}${final.service ? `，${final.service}` : ''}）` : ''}${final.synthesized ? '（APP 补记）' : ''}`)
      try { this.options.onFinished?.({ op, article: session.article, events: session.events, runDone: final }) } catch { /* 回调出错不影响结束 */ }
      return final.exit_code
    })
    return { sessionId: id, done }
  }
}

function defaultWriteMarker(file: string): void { writeFileSync(file, `stop ${new Date().toISOString()}\n`, 'utf8') }
function defaultRemoveMarker(file: string): void { rmSync(file, { force: true }) }
