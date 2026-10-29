import { Eye, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { Task, TaskStatus } from '../../shared/types'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import { STATUS_LABELS } from '../status'

type Filter = 'all' | TaskStatus

export function TaskListPage({ tasks, onOpen }: { tasks: Task[]; onOpen: (id: string) => void }): React.JSX.Element {
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const visible = useMemo(() => tasks.filter((task) =>
    (filter === 'all' || task.status === filter) && task.title.toLowerCase().includes(query.toLowerCase())
  ), [filter, query, tasks])

  return (
    <section>
      <PageHeader title="任务列表" description="Mock 用于模拟测试；Real 使用 Article2Video，当前仅开放示例篇样本。" />
      <div className="toolbar">
        <label className="search-box"><Search size={17} /><span className="sr-only">搜索任务</span><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索任务标题" /></label>
        <div className="filter-row">
          <button className={filter === 'all' ? 'chip active' : 'chip'} onClick={() => setFilter('all')}>全部 {tasks.length}</button>
          {(Object.keys(STATUS_LABELS) as TaskStatus[]).map((status) => (
            <button key={status} className={filter === status ? 'chip active' : 'chip'} onClick={() => setFilter(status)}>
              {STATUS_LABELS[status]} {tasks.filter((task) => task.status === status).length}
            </button>
          ))}
        </div>
      </div>
      <div className="section-card task-table-card">
        {visible.length ? (
          <div className="task-table">
            <div className="task-row task-head"><span>任务标题</span><span>来源</span><span>状态</span><span>进度</span><span>更新时间</span><span>操作</span></div>
            {visible.map((task) => (
              <div className="task-row" key={task.id}>
                <strong>{task.title}</strong><span>{task.source === 'mock' ? 'Mock 样本' : 'Real'}</span><StatusBadge status={task.status} />
                <span>{task.source === 'real' ? `${task.real.steps.filter((step) => ['done', 'skipped'].includes(step.status)).length} / ${task.real.steps.length} 已知步骤` : task.status === 'pending' ? '—' : `${task.progress}%`}</span>
                <span>{formatDate(task.updatedAt)}</span>
                <button className="icon-text-button" onClick={() => onOpen(task.id)}><Eye size={15} /> 查看</button>
              </div>
            ))}
          </div>
        ) : <EmptyState title="没有匹配的任务" description="调整筛选条件，或创建新的 Mock 任务。" />}
      </div>
    </section>
  )
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}
