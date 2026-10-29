import { useEffect, useState } from 'react'
import { errorGuide, type GuideAction } from '../../../shared/v1/error-kinds'
import type { EstimateEvent } from '../../../shared/v1/protocol'
import { runPhase, stepIndex, type RunView } from '../../../shared/v1/run-model'
import { stepName } from '../../../shared/v1/steps'
import { GuideBanner, Preview, ReportSummary, RetryBanner, StepBar } from '../parts'

// 工作台 / 出片进度（原型 D 第三屏、H）。

export interface WorkbenchProps {
  title: string
  view: RunView
  maxCostCny: number
  estimate: EstimateEvent | null
  ended: boolean
  // 点了停止、引擎还在收尾（APP2b）
  stopping?: boolean
  reportExpanded?: boolean
  // APP3：运行环境还没准备好——"继续"类按钮不能点，提示框上面显示准备进度
  resumeBlocked?: boolean
  runtimeNotice?: React.ReactNode
  onAction(action: GuideAction, value?: number): void
  onStop(): void
  onOpenOutput(): void
  onNext(): void
  // APP4：已完成的文章"重新出片"（先估算费用，正常从缓存重建，几乎不花钱）
  onRemake?(): void
}

const pad = (n: number): string => String(n).padStart(2, '0')
export function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

export function Workbench({ title, view, maxCostCny, estimate, ended, stopping, reportExpanded, resumeBlocked = false, runtimeNotice, onAction, onStop, onOpenOutput, onNext, onRemake }: WorkbenchProps): React.JSX.Element {
  const phase = runPhase(view)
  const live = phase === 'running' || phase === 'retrying' || phase === 'idle'
  const [observedAt] = useState(Date.now())
  const now = useNow(live && !ended)
  const [expanded, setExpanded] = useState(reportExpanded ?? phase === 'done_red')
  useEffect(() => { if (reportExpanded !== undefined) setExpanded(reportExpanded) }, [reportExpanded])

  const first = view.firstAt ? Date.parse(view.firstAt) : null
  const last = view.lastAt ? Date.parse(view.lastAt) : null
  const elapsed = first && last ? Math.max(last - first, live ? now - observedAt : 0) : 0
  const s8 = view.steps.find(step => step.id === 'S8')
  const images = s8?.progress ? `${s8.progress.done} / ${s8.progress.total}${s8.state === 'failed' ? ' · 已停' : s8.state === 'retrying' ? ' · 重试中' : ''}` : (s8?.state === 'done' ? '已完成' : '—')
  const failure = view.failure
  const guide = phase === 'failed' || phase === 'stopped'
    ? errorGuide(failure?.errorKind ?? view.runDone?.error_kind, { stepName: stepName(failure?.step, view.contentType), missingKeys: view.runDone?.missing_keys, maxCostCny,
      service: failure?.service ?? view.runDone?.service ?? null })
    : null
  const missing = view.report?.items.filter(item => item.kind === 'illustration_missing') ?? []
  const reds = view.report?.items.filter(item => item.level === 'red') ?? []
  const done = phase === 'done_pass' || phase === 'done_review' || phase === 'done_red' || phase === 'missing_images'
  const state = done
    ? (reds.length ? <span className="state tone-danger">已完成 · {reds.length} 处标红</span> : <span className="state tone-ok">已完成 · 用时 {clock(elapsed)}</span>)
    : phase === 'stopped' ? <span className="state tone-muted">已停止</span>
    : phase === 'failed' ? <span className="state tone-danger">已暂停</span>
    : stopping && !ended ? <span className="state tone-warn">正在停止，收尾中……</span>
    : <span className="state muted">出片中</span>
  const typeText = view.contentType === 'screenshot' ? '截图型（11 步）' : view.contentType === 'theory' ? '理论型（10 步）' : '判断中'
  const remaining = estimate && !done ? ` · 预计共 ¥${Math.round(estimate.low_cny)}～${Math.round(estimate.high_cny)}` : ''
  const head = done ? '全部完成' : `第 ${stepIndex(view)} 步 / 共 ${view.steps.length} 步`

  return (
    <div className="work-main">
      <div className="work-body">
        <div className="work-left">
          <h2 className="work-title">{title}{state}</h2>
          {phase === 'retrying' && view.retry && <RetryBanner attempt={view.retry.attempt} waitSec={view.retry.waitSec} />}
          {guide && resumeBlocked && runtimeNotice}
          {guide && <GuideBanner key={`${guide.title}-${maxCostCny}`} guide={guide} engineMessage={failure?.message} onAction={onAction} blocked={resumeBlocked ? ['resume', 'raise_limit'] : []} />}
          <Preview state={done ? 'done' : guide ? 'blocked' : 'waiting'} title={title}
            videoUrl={done && view.runDone?.delivery_path && /\.mp4$/i.test(view.runDone.delivery_path) ? window.a2v.mediaUrl(view.runDone.delivery_path) : null} />
        </div>
        <aside className="work-side">
          <span className="side-label">本次出片</span>
          <div className="cost-card">
            <span className="label">已花费</span>
            <span className="value">¥{view.spentCny.toFixed(2)}</span>
            <span className="sub">上限 ¥{maxCostCny}{remaining}</span>
          </div>
          <div className="kv"><span className="k">已用时</span><span>{clock(elapsed)}</span></div>
          <div className="kv"><span className="k">文章类型</span><span>{typeText}</span></div>
          <div className="kv"><span className="k">插图</span><span>{missing.length ? `缺 ${missing.length} 张` : images}</span></div>
          {phase === 'missing_images' && (
            <div className="warn-card">
              <span className="title">{missing.length} 张图没出来</span>
              <span>已登记，不会重复扣费。成片里先用前后画面补位了。</span>
              <button className="btn btn-sm" disabled={resumeBlocked} onClick={() => onAction('resume')}>继续补图</button>
            </div>
          )}
          {view.report && done && <ReportSummary report={view.report} expanded={expanded} onToggle={() => setExpanded(!expanded)} />}
          {/* APP2：处理问题的按钮只放在顶部提示框里；右下角出片中只放"停止"，暂停、失败、停止时不放按钮 */}
          <div className={done ? 'side-actions done-acts' : 'side-actions'}>
            {live && (stopping && !ended
              ? <button className="btn btn-danger btn-block" disabled>正在停止…</button>
              : <button className="btn btn-danger btn-block" onClick={onStop}>停止</button>)}
            {done && <>
              <div className="btn-row"><button className="btn" onClick={onOpenOutput}>打开成片文件夹</button>{onRemake && ended && <button className="btn" disabled={resumeBlocked} onClick={onRemake}>重新出片</button>}</div>
              <button className="btn btn-primary btn-block" onClick={onNext}>再做一篇</button>
            </>}
          </div>
        </aside>
      </div>
      <StepBar steps={view.steps} head={head} />
    </div>
  )
}
