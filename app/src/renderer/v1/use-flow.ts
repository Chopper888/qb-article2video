import { useCallback, useEffect, useRef, useState } from 'react'
import type { EngineEvent, EstimateEvent, InputResolvedEvent } from '../../shared/v1/protocol'
import { fillContentType, initialRunView, reduceRunView, type RunView } from '../../shared/v1/run-model'
import { unescapeMarkdown } from '../../shared/v1/title'

// 一篇文章从"粘贴链接"到"出片完成"的流程。所有状态都由引擎事件推出来；
// 只确认一次：确认费用后直接开跑，运行中不再弹确认。

export type Flow =
  | { kind: 'idle' }
  | { kind: 'importing'; input: string; sessionId: string | null; view: RunView }
  | { kind: 'problems'; input: string; view: RunView }
  // waiting：导入完了，运行环境还没准备好（APP3），准备好后自动估算
  | { kind: 'estimating'; input: string; resolved: InputResolvedEvent; sessionId: string | null; waiting?: boolean }
  | { kind: 'confirm'; input: string; resolved: InputResolvedEvent; estimate: EstimateEvent | null }
  | { kind: 'run'; article: string; title: string; sessionId: string | null; view: RunView; maxCostCny: number; estimate: EstimateEvent | null; startedAt: number; ended: boolean; stopping?: boolean }

export interface FlowControls {
  flow: Flow
  startImport(input: string): Promise<void>
  // APP4：一次拖进多个文件（一个 md 加图片）
  startImportDropped(paths: string[]): Promise<void>
  confirmRun(maxCostCny: number): Promise<void>
  startRun(article: string, title: string, maxCostCny: number, estimate?: EstimateEvent | null): Promise<void>
  resume(maxCostCny?: number): Promise<void>
  // 任务列表里"继续"：没有本次的事件，直接续跑这篇
  resumeArticle(article: string, title: string, maxCostCny: number): Promise<void>
  // 已经在工作文件夹里的文章直接估算 → 确认费用（开发截图用：样本文章不经导入）
  estimateArticle(resolved: InputResolvedEvent): Promise<void>
  setMaxCost(maxCostCny: number): void
  stop(): Promise<void>
  reset(): void
}

export function plainError(reason: unknown): string {
  const message = reason instanceof Error ? reason.message : String(reason)
  return message.replace(/^Error invoking remote method '[^']+':\s*/, '').replace(/^Error:\s*/, '')
}

export function useFlow(onError: (message: string) => void, runtimeReady = true): FlowControls {
  const [flow, setFlowState] = useState<Flow>({ kind: 'idle' })
  const readyRef = useRef(runtimeReady)
  readyRef.current = runtimeReady
  // ref 是唯一的真实状态，同步更新；state 只用来触发重新渲染（立即模式下事件和结束会在同一轮里连续到达）
  const flowRef = useRef<Flow>(flow)
  const setFlow = useCallback((next: Flow | ((current: Flow) => Flow)) => {
    flowRef.current = typeof next === 'function' ? next(flowRef.current) : next
    setFlowState(flowRef.current)
  }, [])
  // 事件可能比 invoke 的返回值先到：先按 session 缓存
  const buffered = useRef(new Map<string, EngineEvent[]>())
  const ended = useRef(new Set<string>())

  // 1.0.1：主进程抛出的错误到这里会带上 Electron 的前缀（Error invoking remote method 'v1:…': Error: …），只留给人看的那句话
  const fail = useCallback((reason: unknown) => onError(plainError(reason)), [onError])

  const startEstimate = useCallback((article: string) => {
    void window.a2v.estimate(article).then(id => {
      setFlow(next => next.kind === 'estimating' ? { ...next, sessionId: id, waiting: false } : next)
      for (const event of buffered.current.get(id) ?? []) apply(id, event)
      buffered.current.delete(id)
    }).catch(fail)
  // apply 在下面定义（依赖为空、不会变），这里调用时已经有值
  }, [fail])

  // APP3：运行环境准备好了，补上等着的估算
  useEffect(() => {
    const current = flowRef.current
    if (runtimeReady && current.kind === 'estimating' && current.waiting) {
      setFlow({ ...current, waiting: false })
      startEstimate(current.resolved.article)
    }
  }, [runtimeReady, setFlow, startEstimate])

  const apply = useCallback((sessionId: string, event: EngineEvent) => {
    setFlow(current => {
      if (current.kind === 'importing' && current.sessionId === sessionId) return { ...current, view: reduceRunView(current.view, event) }
      if (current.kind === 'estimating' && current.sessionId === sessionId && event.event === 'estimate') {
        return { kind: 'confirm', input: current.input, resolved: current.resolved, estimate: event }
      }
      if (current.kind === 'run' && current.sessionId === sessionId) return { ...current, view: reduceRunView(current.view, event) }
      return current
    })
  }, [])

  // APP4：完成页的"文章类型"以账本和 S0 结果为准（这次运行跳过了 S0 时引擎不再发类型）
  const backfillType = useCallback((article: string) => {
    void window.a2v.articleInfo(article).then(info => {
      if (!info?.contentType) return
      setFlow(next => next.kind === 'run' && next.article === article ? { ...next, view: fillContentType(next.view, info.contentType) } : next)
    }, () => undefined)
  }, [setFlow])

  const finished = useCallback((sessionId: string) => {
    const current = flowRef.current
    if (current.kind === 'importing' && current.sessionId === sessionId) {
      const { view } = current
      if (view.problems?.length || !view.resolved) { setFlow({ kind: 'problems', input: current.input, view }); return }
      const resolved = view.resolved
      setFlow({ kind: 'estimating', input: current.input, resolved, sessionId: null, waiting: !readyRef.current })
      if (readyRef.current) startEstimate(resolved.article)
      return
    }
    if (current.kind === 'estimating' && current.sessionId === sessionId) {
      setFlow({ kind: 'confirm', input: current.input, resolved: current.resolved, estimate: null })
      return
    }
    if (current.kind === 'run' && current.sessionId === sessionId) {
      const { article } = current
      setFlow(next => next.kind === 'run' ? { ...next, ended: true } : next)
      backfillType(article)
      void window.a2v.readReport(article).then(report => setFlow(next => next.kind === 'run' && next.sessionId === sessionId ? { ...next, view: { ...next.view, report } } : next)).catch(fail)
    }
  }, [apply, fail, backfillType])

  useEffect(() => {
    const offEvent = window.a2v.onSessionEvent(({ sessionId, event }) => {
      const current = flowRef.current
      const known = ('sessionId' in current) && current.sessionId === sessionId
      if (!known) { buffered.current.set(sessionId, [...(buffered.current.get(sessionId) ?? []), event]); return }
      apply(sessionId, event)
    })
    const offEnd = window.a2v.onSessionEnd(({ sessionId }) => {
      const current = flowRef.current
      if (('sessionId' in current) && current.sessionId === sessionId) finished(sessionId)
      else ended.current.add(sessionId)
    })
    return () => { offEvent(); offEnd() }
  }, [apply, finished])

  // invoke 返回 sessionId 之后：补放缓存的事件、补处理已经结束的 session
  const adopt = useCallback((sessionId: string) => {
    for (const event of buffered.current.get(sessionId) ?? []) apply(sessionId, event)
    buffered.current.delete(sessionId)
    if (ended.current.delete(sessionId)) {
      finished(sessionId)
    }
  }, [apply, finished])

  const startImport = useCallback(async (input: string) => {
    setFlow({ kind: 'importing', input, sessionId: null, view: initialRunView() })
    try {
      const id = await window.a2v.importArticle(input)
      setFlow(current => current.kind === 'importing' ? { ...current, sessionId: id } : current)
      adopt(id)
    } catch (reason) { setFlow({ kind: 'idle' }); fail(reason) }
  }, [adopt, fail])

  // 拖进来的多个文件：Main 先检查（没有 md、多个 md 直接提示，不导入），再复制进 _a2v_import/ 的新文件夹导入；
  // 之后"重新检查"按那个文件夹来
  const startImportDropped = useCallback(async (paths: string[]) => {
    setFlow({ kind: 'importing', input: paths[0] ?? '', sessionId: null, view: initialRunView() })
    try {
      const { sessionId, input } = await window.a2v.importDropped(paths)
      setFlow(current => current.kind === 'importing' ? { ...current, input, sessionId } : current)
      adopt(sessionId)
    } catch (reason) { setFlow({ kind: 'idle' }); fail(reason) }
  }, [adopt, fail])

  const startRun = useCallback(async (article: string, title: string, maxCostCny: number, estimate: EstimateEvent | null = null, mode: 'run' | 'resume' = 'run') => {
    // 类型先用已知的（上一次运行、导入结果）；S0 被跳过时不会再发类型
    const current = flowRef.current
    const type = current.kind === 'run' ? current.view.contentType : current.kind === 'confirm' ? current.resolved.content_type : null
    const next: Flow = { kind: 'run', article, title: unescapeMarkdown(title), sessionId: null, view: initialRunView(type), maxCostCny, estimate, startedAt: Date.now(), ended: false }
    setFlow(next)
    if (!type) backfillType(article)
    try {
      const id = mode === 'run' ? await window.a2v.run(article, maxCostCny) : await window.a2v.resume(article, maxCostCny)
      setFlow(current => current.kind === 'run' ? { ...current, sessionId: id } : current)
      adopt(id)
    } catch (reason) { fail(reason) }
  }, [adopt, fail, backfillType])

  const confirmRun = useCallback(async (maxCostCny: number) => {
    const current = flowRef.current
    if (current.kind !== 'confirm') return
    await startRun(current.resolved.article, current.resolved.title, maxCostCny, current.estimate)
  }, [startRun])

  const resume = useCallback(async (maxCostCny?: number) => {
    const current = flowRef.current
    if (current.kind !== 'run') return
    await startRun(current.article, current.title, maxCostCny ?? current.maxCostCny, current.estimate, 'resume')
  }, [startRun])

  const estimateArticle = useCallback(async (resolved: InputResolvedEvent) => {
    setFlow({ kind: 'estimating', input: resolved.work_dir, resolved, sessionId: null })
    try {
      const id = await window.a2v.estimate(resolved.article)
      setFlow(next => next.kind === 'estimating' ? { ...next, sessionId: id } : next)
      adopt(id)
    } catch (reason) { setFlow({ kind: 'idle' }); fail(reason) }
  }, [adopt, fail])

  const resumeArticle = useCallback(async (article: string, title: string, maxCostCny: number) => {
    await startRun(article, title, maxCostCny, null, 'resume')
  }, [startRun])

  const setMaxCost = useCallback((maxCostCny: number) => {
    setFlow(current => current.kind === 'run' ? { ...current, maxCostCny } : current)
  }, [])

  // APP2b：点了停止就进入"正在停止，收尾中"（引擎最多 30 秒内自己收尾，超时主进程才强制结束）
  const stop = useCallback(async () => {
    const current = flowRef.current
    if (current.kind !== 'run' || !current.sessionId || current.stopping || current.ended) return
    const id = current.sessionId
    setFlow(next => next.kind === 'run' && next.sessionId === id ? { ...next, stopping: true } : next)
    await window.a2v.stop(id).catch(fail)
  }, [fail])

  const reset = useCallback(() => setFlow({ kind: 'idle' }), [])

  return { flow, startImport, startImportDropped, confirmRun, startRun: (a, t, m, e) => startRun(a, t, m, e), resume, resumeArticle, estimateArticle, setMaxCost, stop, reset }
}
