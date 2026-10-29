import type { ArticleEngineAdapter, ArticleRequest, EngineSession, EventSink, ImportRequest, RunRequest } from '../../shared/v1/adapter'
import type { CheckReport } from '../../shared/v1/protocol'

// 普通用户固定走真实引擎；开发模式下可以在开发者开关里切到演示场景（截图、测试用）。
// 停止按 session 的来源分：演示 session 的 id 以 demo- 开头。
export class RoutedEngineAdapter implements ArticleEngineAdapter {
  readonly kind = 'real' as const

  constructor(private readonly real: ArticleEngineAdapter, private readonly demo: ArticleEngineAdapter | null,
    private readonly mode: () => 'real' | 'demo') {}

  private pick(): ArticleEngineAdapter { return this.demo && this.mode() === 'demo' ? this.demo : this.real }

  importArticle(request: ImportRequest, onEvent: EventSink): EngineSession { return this.pick().importArticle(request, onEvent) }
  estimate(request: ArticleRequest, onEvent: EventSink): EngineSession { return this.pick().estimate(request, onEvent) }
  run(request: RunRequest, onEvent: EventSink): EngineSession { return this.pick().run(request, onEvent) }
  resume(request: RunRequest, onEvent: EventSink): EngineSession { return this.pick().resume(request, onEvent) }
  stop(sessionId: string): Promise<void> { return (this.demo && sessionId.startsWith('demo-') ? this.demo : this.real).stop(sessionId) }
  readReport(request: ArticleRequest): Promise<CheckReport | null> { return this.pick().readReport(request) }
}
