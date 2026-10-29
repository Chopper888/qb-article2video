import { Clapperboard, FileVideo2, LayoutDashboard, ListVideo, Plus, Settings } from 'lucide-react'
import type { AppPage } from '../../shared/types'
import { useEnvironmentReadiness } from './EnvironmentReadiness'

interface SidebarProps {
  page: AppPage
  onNavigate: (page: AppPage) => void
}

const links: Array<{ page: AppPage; label: string; icon: typeof LayoutDashboard }> = [
  { page: 'dashboard', label: '工作台', icon: LayoutDashboard },
  { page: 'tasks', label: '任务列表', icon: ListVideo },
  { page: 'outputs', label: '输出列表', icon: FileVideo2 },
  { page: 'settings', label: '设置', icon: Settings }
]

export function Sidebar({ page, onNavigate }: SidebarProps): React.JSX.Element {
  const { result, loading } = useEnvironmentReadiness()
  const status = loading ? 'loading' : result?.status === 'ready' ? 'ready' : 'blocked'
  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="brand-mark"><Clapperboard size={23} /></span>
        <span><strong>文章成片</strong><small>Mock + Real · v0.1.0</small></span>
      </div>
      <button className="new-task-button" onClick={() => onNavigate('create')}>
        <Plus size={18} /> 新建任务
      </button>
      <nav aria-label="主导航">
        {links.map(({ page: target, label, icon: Icon }) => (
          <button
            key={target}
            className={page === target ? 'nav-item active' : 'nav-item'}
            onClick={() => onNavigate(target)}
          >
            <Icon size={19} /> {label}
          </button>
        ))}
      </nav>
      <div className="sidebar-note" data-readiness={status} aria-live="polite">
        <span className={`engine-dot ${status}`} />
        <div><strong>Real Engine</strong><small>{status === 'loading' ? '正在检查运行环境' : status === 'ready' ? '运行环境已就绪' : '运行环境未就绪'}</small></div>
      </div>
    </aside>
  )
}
