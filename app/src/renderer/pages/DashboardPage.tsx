import { ArrowRight, Clock3, PlayCircle } from 'lucide-react'
import type { AppState, Task } from '../../shared/types'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { ProgressBar } from '../components/ProgressBar'
import { StatusBadge } from '../components/StatusBadge'

interface DashboardProps {
  state: AppState
  onCreate: () => void
  onOpenTask: (id: string) => void
  onOpenOutputs: () => void
}

export function DashboardPage({ state, onCreate, onOpenTask, onOpenOutputs }: DashboardProps): React.JSX.Element {
  const running = state.tasks.find((task) => task.status === 'running')
  const queued = state.tasks.filter((task) => task.status === 'queued')
  const recent = state.tasks.slice(0, 4)
  const outputs = state.outputs.slice(0, 3)

  return (
    <section>
      <PageHeader title="工作台" description="Mock 用于模拟测试；Real 使用 Article2Video，当前仅开放示例篇样本。" />
      <div className="metric-grid">
        <Metric value={running ? 1 : 0} label="运行中" tone="blue" />
        <Metric value={queued.length} label="等待中" tone="orange" />
        <Metric value={state.tasks.filter((task) => task.status === 'pending').length} label="待开始" tone="gray" />
        <Metric value={state.outputs.length} label={state.outputs.some((output) => !('mock' in output)) ? '输出记录' : '模拟输出'} tone="green" />
      </div>

      <div className="section-card hero-card">
        <SectionTitle title="当前运行任务" />
        {running ? (
          <div className="running-task">
            <div className="task-glyph"><PlayCircle size={28} /></div>
            <div className="grow">
              <div className="row-between"><h3>{running.title}</h3><StatusBadge status={running.status} /></div>
              <p>{running.currentStep ? `正在执行：${running.currentStep}` : running.source === 'mock' ? '准备 Mock 流程' : '准备 Real 任务'}</p>
              {running.source === 'mock' ? <ProgressBar value={running.progress} /> : <p>Real 引擎：已记录 {running.real.steps.length} 个步骤，非工作量进度</p>}
            </div>
            <button className="secondary-button" onClick={() => onOpenTask(running.id)}>查看详情</button>
          </div>
        ) : (
          <EmptyState
            title="当前没有运行中的任务"
            description="可创建 Mock 开发测试任务，或前往新建任务创建示例篇 Real 联调任务。"
            action={<button className="primary-button" onClick={onCreate}>新建模拟任务</button>}
          />
        )}
      </div>

      <div className="dashboard-columns">
        <div className="section-card">
          <SectionTitle title={`等待中的任务（${queued.length}）`} />
          {queued.length ? queued.map((task) => <CompactTask key={task.id} task={task} onOpen={onOpenTask} />) :
            <p className="muted-line">暂无等待任务</p>}
        </div>
        <div className="section-card">
          <SectionTitle title="最近任务" />
          {recent.length ? recent.map((task) => <CompactTask key={task.id} task={task} onOpen={onOpenTask} />) :
            <p className="muted-line">尚未创建任务</p>}
        </div>
      </div>

      <div className="section-card">
        <div className="row-between">
          <SectionTitle title="最近输出" />
          <button className="text-button" onClick={onOpenOutputs}>查看全部 <ArrowRight size={15} /></button>
        </div>
        {outputs.length ? (
          <div className="output-preview-row">
            {outputs.map((output, index) => (
              <button className={`mini-output tone-${index % 3}`} key={output.id} onClick={() => onOpenTask(output.taskId)}>
                <span className="mock-ribbon">{'mock' in output ? '模拟输出' : 'Real 输出路径'}</span>
                <strong>{output.title}</strong>
                <small>{'mock' in output ? `${output.durationLabel} · 1080P` : output.deliveryPath}</small>
              </button>
            ))}
          </div>
        ) : <p className="muted-line">任务完成后，输出记录会显示在这里。</p>}
      </div>
    </section>
  )
}

function Metric({ value, label, tone }: { value: number; label: string; tone: string }): React.JSX.Element {
  return <div className={`metric-card ${tone}`}><strong>{value}</strong><span>{label}</span></div>
}

function SectionTitle({ title }: { title: string }): React.JSX.Element {
  return <h2 className="section-title">{title}</h2>
}

function CompactTask({ task, onOpen }: { task: Task; onOpen: (id: string) => void }): React.JSX.Element {
  return (
    <button className="compact-task" onClick={() => onOpen(task.id)}>
      <span><strong>{task.title}</strong><small><Clock3 size={12} /> {formatDate(task.updatedAt)}</small></span>
      <StatusBadge status={task.status} />
    </button>
  )
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}
