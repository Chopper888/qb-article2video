import type {
  ArticleEngineAdapter, ArticleRequest, DemoImportScenario, DemoRunScenario, EngineSession, EventSink, ImportRequest, RunRequest
} from '../../shared/v1/adapter'
import type { CheckReport, EngineEvent, RunDoneEvent } from '../../shared/v1/protocol'
import { estimateScript, importScript, runScript, stopEvent, type DemoScript } from './demo-scenarios'

// 演示实现：按场景脚本定时推事件。不启动任何进程、不联网、不花钱。
export interface DemoSettings {
  importScenario: DemoImportScenario
  runScenario: DemoRunScenario
  // 0 = 立即放完（截图、测试用）；其余是每条事件之间的毫秒数
  delayMs: number
  // APP2：开发者开关里切"真实引擎 / 演示场景"（APP4："不付费试跑"挪到 DevNoPaid，只在内存里）
  mode: 'real' | 'demo'
}

interface Session {
  article: string
  lastSeq: number
  timers: ReturnType<typeof setTimeout>[]
  done: boolean
  onEvent: EventSink
  finish(code: number | null): void
}

export class DemoEngineAdapter implements ArticleEngineAdapter {
  readonly kind = 'demo' as const
  private counter = 0
  private readonly sessions = new Map<string, Session>()
  private lastReport: CheckReport | null = null

  constructor(public settings: DemoSettings = { importScenario: 'import_pass', runScenario: 'run_screenshot', delayMs: 350, mode: 'real' }) {}

  importArticle(_request: ImportRequest, onEvent: EventSink): EngineSession {
    return this.play(importScript(this.settings.importScenario, this.settings.delayMs), onEvent)
  }

  estimate(_request: ArticleRequest, onEvent: EventSink): EngineSession {
    return this.play(estimateScript(this.settings.importScenario, this.settings.delayMs), onEvent)
  }

  run(_request: RunRequest, onEvent: EventSink): EngineSession {
    return this.play(runScript(this.settings.runScenario, this.settings.delayMs), onEvent)
  }

  // 演示的续跑：放"完成"的脚本
  resume(_request: RunRequest, onEvent: EventSink): EngineSession {
    return this.play(runScript('done_pass', this.settings.delayMs), onEvent)
  }

  async stop(sessionId: string): Promise<void> {
    const session = this.sessions.get(sessionId)
    if (!session || session.done) return
    for (const timer of session.timers) clearTimeout(timer)
    session.onEvent(stopEvent(session.article, session.lastSeq))
    session.done = true
    session.finish(3)
  }

  async readReport(_request: ArticleRequest): Promise<CheckReport | null> {
    return this.lastReport
  }

  dispose(): void {
    for (const session of this.sessions.values()) for (const timer of session.timers) clearTimeout(timer)
    this.sessions.clear()
  }

  private play(script: DemoScript, onEvent: EventSink): EngineSession {
    const sessionId = `demo-${++this.counter}`
    this.lastReport = script.report
    const first = script.steps.find(step => 'article' in step.event)?.event as { article?: string } | undefined
    let finish: (code: number | null) => void = () => undefined
    const done = new Promise<number | null>(resolve => { finish = resolve })
    const session: Session = { article: first?.article ?? '', lastSeq: 0, timers: [], done: false, onEvent, finish }
    this.sessions.set(sessionId, session)
    const emit = (event: EngineEvent): void => {
      if (session.done) return
      session.lastSeq = event.seq
      onEvent(event)
      if (event.event === 'run_done') { session.done = true; finish((event as RunDoneEvent).exit_code) }
    }
    if (this.settings.delayMs <= 0) {
      // 立即模式也放到下一轮事件循环，调用方先拿到 sessionId
      queueMicrotask(() => { for (const step of script.steps) emit(step.event) })
    } else {
      let at = 0
      for (const step of script.steps) {
        at += step.delayMs
        session.timers.push(setTimeout(() => emit(step.event), at))
      }
    }
    return { sessionId, done }
  }
}
