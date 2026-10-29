import { Clapperboard, Clock, Home, List, Moon, RotateCw, Settings, Sun } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import type { ErrorGuide, GuideAction } from '../../shared/v1/error-kinds'
import type { CheckReport } from '../../shared/v1/protocol'
import type { StepView } from '../../shared/v1/run-model'
import type { RuntimePrep } from '../../shared/v1/types'

export type Page = 'home' | 'tasks' | 'library' | 'settings'

export function Logo({ large = false }: { large?: boolean }): React.JSX.Element {
  const size = large ? 26 : 13
  return (
    <span className={large ? 'logo logo-lg' : 'logo'} aria-hidden="true">
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M4 5h11v14H4z" /><path d="M15 10l5-3v10l-5-3" /></svg>
    </span>
  )
}

export function ThemeButton({ theme, onToggle }: { theme: 'dark' | 'light'; onToggle: () => void }): React.JSX.Element {
  return <button className="theme-btn" onClick={onToggle} aria-label="切换浅色或深色">{theme === 'dark' ? <><Sun size={13} style={{ verticalAlign: -2 }} /> 浅色</> : <><Moon size={13} style={{ verticalAlign: -2 }} /> 深色</>}</button>
}

export function TopBar({ page, onNavigate, engine, right }: { page: Page; onNavigate: (page: Page) => void; engine: ReactNode; right?: ReactNode }): React.JSX.Element {
  const items: [Page, string][] = [['home', '新建'], ['tasks', '任务'], ['library', '成片库'], ['settings', '设置']]
  return (
    <header className="topbar">
      <div className="brand"><Logo />文章成片</div>
      <nav className="nav" aria-label="主导航">
        {items.map(([id, label]) => <button key={id} aria-current={page === id ? 'page' : undefined} onClick={() => onNavigate(id)}>{label}</button>)}
      </nav>
      <div className="topbar-right">{engine}{right}</div>
    </header>
  )
}

export function EngineStatus({ version, ready }: { version: string | null; ready: boolean }): React.JSX.Element {
  return <span className="engine-pill"><span className={ready ? 'dot' : 'dot danger'} />引擎 {version ?? '未知版本'} · {ready ? '就绪' : '找不到'}</span>
}

export function Rail({ onNavigate, theme }: { onNavigate: (page: Page) => void; theme?: ReactNode }): React.JSX.Element {
  return (
    <nav className="rail" aria-label="工作台导航">
      <button className="rail-home" aria-label="回到启动页" onClick={() => onNavigate('home')}><Home size={20} /></button>
      <button aria-label="任务" onClick={() => onNavigate('tasks')}><List size={20} /></button>
      <button aria-label="成片库" onClick={() => onNavigate('library')}><Clapperboard size={20} /></button>
      <button aria-label="设置" onClick={() => onNavigate('settings')}><Settings size={20} /></button>
      <span className="spacer" />
      {theme}
    </nav>
  )
}

export function RailThemeButton({ theme, onToggle }: { theme: 'dark' | 'light'; onToggle: () => void }): React.JSX.Element {
  return <button aria-label="切换浅色或深色" onClick={onToggle}>{theme === 'dark' ? <Sun size={20} /> : <Moon size={20} />}</button>
}

function stepMeta(step: StepView): string {
  if (step.state === 'done') return '完成'
  if (step.state === 'skipped') return '已有，跳过'
  if (step.state === 'waiting') return '等待'
  const p = step.progress
  const amount = p && p.total > 0 ? (p.unit === '%' ? `${p.done}%` : `${p.done} / ${p.total}${p.unit === '张' ? '' : ` ${p.unit}`}`) : ''
  if (step.state === 'failed') return amount ? `${amount} · 已停` : '已停'
  if (step.state === 'retrying') return amount ? `${amount} · 重试中` : '重试中'
  return amount ? (p?.label && p.unit !== '%' ? `${p.label} ${amount}` : amount) : '进行中'
}

export function StepBar({ steps, head }: { steps: StepView[]; head: string }): React.JSX.Element {
  return (
    <footer className="flow" aria-label="出片流程">
      <div className="flow-head"><span>出片流程</span><span>{head}</span></div>
      <div className="flow-steps" style={{ gridTemplateColumns: `repeat(${steps.length}, minmax(0, 1fr))` }}>
        {steps.map(step => (
          <div key={step.id} className={`flow-step ${step.state}`} data-step={step.id}>
            <span className="n">{step.name}</span>
            <span className="m">{stepMeta(step)}</span>
          </div>
        ))}
      </div>
    </footer>
  )
}

// APP2b：出错 / 暂停提示里的"引擎原文"默认收起，一行小字"查看详情"，点开可选中复制，再点收起
export function EngineDetail({ message }: { message: string }): React.JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <div className="engine-detail">
      <button type="button" className="link-btn detail-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起' : '查看详情'}</button>
      {open && <div className="raw selectable">引擎原文：{message}</div>}
    </div>
  )
}

// blocked：暂时不能点的动作（APP3：运行环境准备好之前"继续"类的按钮不能点）
export function GuideBanner({ guide, engineMessage, onAction, blocked = [] }: { guide: ErrorGuide; engineMessage?: string | null; onAction: (action: GuideAction, value?: number) => void; blocked?: GuideAction[] }): React.JSX.Element {
  const [limit, setLimit] = useState(guide.limitInput?.defaultValue ?? 0)
  const valid = Number.isFinite(limit) && limit > 0 && limit <= 10000
  if (guide.limitInput) {
    // 到费用上限：一个金额输入框 + 一个按钮"上限改成 ¥N，继续"
    return (
      <div className="banner warn-text" role="alert">
        <span className="mark">!</span>
        <div className="body">
          <div className="title">{guide.title}</div>
          <div className="text">{guide.text}</div>
          {guide.showEngineMessage && engineMessage && <EngineDetail message={engineMessage} />}
        </div>
        <div className="actions">
          <label className="limit-edit">新上限 ¥<input className="text-input limit-input" type="number" min={1} value={Number.isFinite(limit) ? limit : ''} aria-label="新的费用上限" onChange={event => setLimit(Number(event.target.value))} /></label>
          <button className="btn btn-primary" disabled={!valid || blocked.includes(guide.primary.action)} onClick={() => onAction(guide.primary.action, limit)}>{guide.primary.label.replace('{N}', valid ? String(limit) : '…')}</button>
        </div>
      </div>
    )
  }
  return (
    <div className={guide.tone === 'danger' ? 'banner danger' : 'banner warn-text'} role="alert">
      <span className="mark">!</span>
      <div className="body">
        <div className="title">{guide.title}</div>
        <div className="text">{guide.text}</div>
        {guide.showEngineMessage && engineMessage && <EngineDetail message={engineMessage} />}
      </div>
      <div className="actions">
        <button className="btn btn-primary" disabled={blocked.includes(guide.primary.action)} onClick={() => onAction(guide.primary.action)}>{guide.primary.label}</button>
        {guide.secondary && <button className="btn" disabled={blocked.includes(guide.secondary.action)} onClick={() => onAction(guide.secondary!.action)}>{guide.secondary.label}</button>}
      </div>
    </div>
  )
}

// APP3：安装版首次启动准备运行环境（复制中带进度；失败显示原因和"重试"）。准备好了或开发模式不显示
export function RuntimePrepNotice({ prep, onRetry }: { prep: RuntimePrep | null; onRetry(): void }): React.JSX.Element | null {
  if (!prep || prep.status === 'ready' || prep.status === 'not_needed') return null
  if (prep.status === 'failed') {
    return (
      <div className="banner danger runtime-prep" role="alert">
        <span className="mark">!</span>
        <div className="body">
          <div className="title">运行环境没准备好，暂时不能估算和出片</div>
          <div className="text">{prep.error}</div>
        </div>
        <div className="actions"><button className="btn btn-primary" onClick={onRetry}>重试</button></div>
      </div>
    )
  }
  return (
    <div className="banner warn-text runtime-prep" role="status" aria-label="正在准备运行环境">
      <span className="mark"><Clock size={15} /></span>
      <div className="body">
        <div className="title">首次启动，正在准备运行环境（约半分钟）……</div>
        <div className="prep-bar" aria-hidden="true"><span style={{ width: `${prep.percent}%` }} /></div>
        <div className="text">{prep.percent}%{prep.totalFiles ? `（${prep.doneFiles.toLocaleString('zh-CN')} / ${prep.totalFiles.toLocaleString('zh-CN')} 个文件）` : ''} · 准备好之前可以先导入文章、填密钥</div>
      </div>
    </div>
  )
}

export function RetryBanner({ attempt, waitSec }: { attempt: number; waitSec: number }): React.JSX.Element {
  return (
    <div className="banner warn-text" role="status">
      <span className="mark"><RotateCw size={15} /></span>
      <div className="body">
        <div className="title">网络不稳定，正在自动重试（第 {attempt} 次）</div>
        <div className="text">服务暂时连不上，{waitSec} 秒后自动再试。不用操作，也不会重复扣费。</div>
      </div>
    </div>
  )
}

export function Preview({ state, title, videoUrl }: { state: 'waiting' | 'blocked' | 'done'; title?: string; videoUrl?: string | null }): React.JSX.Element {
  return (
    <div className="preview">
      <div className="preview-inner">
        {state === 'done' && videoUrl ? (
          <video src={videoUrl} controls preload="metadata" aria-label="成片预览" />
        ) : state === 'done' ? (
          <div className="still">
            <div className="t1">{title}</div>
            <div className="t2">这次没有生成成片文件（没有渲染，或成片不在这台电脑上），没法播放</div>
          </div>
        ) : state === 'blocked' ? <span>处理好上面的问题后，点"继续"接着做</span> : <><Clock size={34} strokeWidth={1.6} /><span>成片渲染完成后在这里播放</span></>}
      </div>
    </div>
  )
}

export function ReportSummary({ report, expanded, onToggle }: { report: CheckReport; expanded: boolean; onToggle: () => void }): React.JSX.Element {
  const reds = report.items.filter(item => item.level === 'red')
  const notices = report.items.filter(item => item.level === 'notice')
  const headline = reds.length ? `${reds.length} 处标红` : notices.length ? `通过 · ${notices.length} 条提示` : '通过'
  const shown = (items: CheckReport['items']): CheckReport['items'] => expanded ? items : items.slice(0, 2)
  return (
    <section className="report" aria-label="检查报告">
      <h3>检查报告 · <span className={reds.length ? 'tone-danger' : 'tone-ok'}>{headline}</span></h3>
      <div className={expanded ? 'scroll' : undefined}>
        {reds.length > 0 && <div className="group-title">标红（需要处理）</div>}
        {shown(reds).map((item, index) => <div className="item red" key={`r${index}`}><span className="time">{item.time ?? '—'}</span><span className="msg">{item.message}</span></div>)}
        {notices.length > 0 && (expanded || reds.length < 2) && <div className="group-title">提示（不影响使用）</div>}
        {(expanded || reds.length < 2) && shown(notices).slice(0, expanded ? undefined : 2 - Math.min(reds.length, 2)).map((item, index) => <div className="item" key={`n${index}`}><span className="time">{item.time ?? '—'}</span><span className="msg">{item.message}</span></div>)}
        {!report.items.length && <div className="muted">没有发现问题。</div>}
      </div>
      {report.items.length > 2 && <button className="link" style={{ alignSelf: 'flex-start' }} onClick={onToggle}>{expanded ? '收起' : '查看完整报告'}</button>}
    </section>
  )
}

export function Toast({ text }: { text: string | null }): React.JSX.Element | null {
  return text ? <div className="toast" role="status">{text}</div> : null
}
