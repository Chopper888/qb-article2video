import { AlertCircle, ArrowLeft, CheckCircle2, Clock3, FileVideo2, RotateCcw, Square, Terminal } from 'lucide-react'
import type { MockTask, Task } from '../../shared/types'
import { RealTaskDetail } from './RealTaskDetail'
import { useEffect, useState } from 'react'
import { ProgressBar } from '../components/ProgressBar'
import { StatusBadge } from '../components/StatusBadge'

const STEPS = ['内容类型判断', '口播', '分组', '配音', '对齐', '图片定位', '编排', '场景构建', '成片渲染']

interface Props {
  task: MockTask
  onBack: () => void
  onStart: () => Promise<void>
  onStop: () => Promise<void>
  onRetry: () => Promise<void>
  onOutputs: () => void
}

export function TaskDetailPage(props: Omit<Props, 'task'> & { task: Task }): React.JSX.Element {
  return props.task.source === 'real'
    ? <RealTaskDetail task={props.task} onBack={props.onBack} onStart={props.onStart} onStop={props.onStop} onOutputs={props.onOutputs} />
    : <MockTaskDetail {...props} task={props.task} />
}

function MockTaskDetail({ task, onBack, onStart, onStop, onRetry, onOutputs }: Props): React.JSX.Element {
  const [now, setNow] = useState(Date.now())
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (task.status !== 'running') return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [task.status])
  const act = async (action: () => Promise<void>): Promise<void> => {
    setBusy(true)
    try { await action() } finally { setBusy(false) }
  }
  const seconds = task.startedAt ? Math.max(0, Math.floor(((task.finishedAt ? Date.parse(task.finishedAt) : now) - Date.parse(task.startedAt)) / 1000)) : 0
  const canStart = ['pending', 'stopped', 'failed', 'completed', 'quality_issue'].includes(task.status)
  const canStop = task.status === 'running' || task.status === 'queued'
  const hasOutput = task.status === 'completed' || task.status === 'quality_issue'
  return (
    <section>
      <button className="back-button" onClick={onBack}><ArrowLeft size={17} /> 返回任务列表</button>
      <header className="detail-header">
        <div><div className="title-with-badge"><h1>{task.title}</h1><StatusBadge status={task.status} /></div><p>来源：Mock 测试样本 · 场景：{scenarioName(task.scenario)}</p></div>
        <div className="action-row">
          {canStart && <button disabled={busy} className="primary-button" onClick={() => void act(task.status === 'pending' ? onStart : onRetry)}><RotateCcw size={16} />{task.status === 'pending' ? '开始生成' : '重新运行'}</button>}
          {canStop && <button disabled={busy} className="danger-button" onClick={() => void act(onStop)}><Square size={15} />{task.status === 'queued' ? '取消等待' : '停止任务'}</button>}
          {hasOutput && <button className="secondary-button" onClick={onOutputs}><FileVideo2 size={16} /> 打开对应输出</button>}
        </div>
      </header>

      {task.status === 'quality_issue' && <div className="notice warning"><AlertCircle /> <span><strong>已经生成结果，但检查发现问题</strong><small>这是 Mock 场景，与执行失败不同。</small></span></div>}
      {task.status === 'failed' && <div className="notice error"><AlertCircle /> <span><strong>Mock 执行失败</strong><small>{task.error}</small></span></div>}
      {task.status === 'stopped' && <div className="notice neutral"><Square /> <span><strong>任务已停止</strong><small>可重新开始这个 Mock 任务。</small></span></div>}

      <div className="section-card progress-card">
        <div className="row-between"><div><h2>{task.currentStep ?? statusSummary(task)}</h2><p>{task.status === 'running' ? 'Mock Engine 正在推进模拟步骤' : '当前任务状态已保存到本地'}</p></div><Clock3 /></div>
        <ProgressBar value={task.progress} />
        <div className="step-strip">
          {STEPS.map((step, index) => {
            const threshold = Math.round(((index + 1) / STEPS.length) * 100)
            const done = task.progress >= threshold
            const current = task.currentStep === step
            return <div key={step} className={done ? 'step done' : current ? 'step current' : 'step'}><span>{done ? <CheckCircle2 size={15} /> : index + 1}</span><small>{step}</small></div>
          })}
        </div>
      </div>

      <div className="detail-grid">
        <div className="section-card log-card">
          <h2 className="section-title"><Terminal size={18} /> 运行日志</h2>
          <div className="log-list">
            {task.logs.length ? task.logs.map((log) => (
              <div className={`log-line ${log.level}`} key={log.id}><time>{formatTime(log.time)}</time><span>{log.message}</span></div>
            )) : <p className="muted-line">尚无日志</p>}
          </div>
        </div>
        <div className="section-card task-meta">
          <h2 className="section-title">任务信息</h2>
          <Meta label="任务 ID" value={task.id.slice(0, 8)} />
          <Meta label="创建时间" value={formatDate(task.createdAt)} />
          <Meta label="当前步骤" value={task.currentStep ?? '—'} />
          <Meta label="已用时间" value={`${seconds} 秒（Mock）`} />
          <Meta label="Mock 类型" value={scenarioName(task.scenario)} />
          <Meta label="输出" value={hasOutput ? '模拟输出（非 MP4）' : '尚无'} />
        </div>
      </div>
    </section>
  )
}

function Meta({ label, value }: { label: string; value: string }): React.JSX.Element {
  return <div className="meta-row"><span>{label}</span><strong>{value}</strong></div>
}
function scenarioName(value: MockTask['scenario']): string { return ({ success: '正常完成', quality_issue: '出片有问题', failure: '执行失败' })[value] }
function statusSummary(task: MockTask): string { return ({ pending: '等待开始', queued: '等待运行位置', running: '运行中', completed: '模拟生成完成', quality_issue: '模拟检查发现问题', failed: '执行失败', stopped: '任务已停止', interrupted: '任务已中断' })[task.status] }
function formatTime(value: string): string { return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value)) }
function formatDate(value: string): string { return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
