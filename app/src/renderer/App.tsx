import { LoaderCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type { AppPage, AppSettings, AppState, CreateTaskInput, OutputView } from '../shared/types'
import { Sidebar } from './components/Sidebar'
import { EnvironmentReadinessProvider } from './components/EnvironmentReadiness'
import { CreateTaskPage } from './pages/CreateTaskPage'
import { DashboardPage } from './pages/DashboardPage'
import { OutputsPage } from './pages/OutputsPage'
import { SettingsPage } from './pages/SettingsPage'
import { TaskDetailPage } from './pages/TaskDetailPage'
import { TaskListPage } from './pages/TaskListPage'

export function App(): React.JSX.Element {
  return <EnvironmentReadinessProvider><AppContent /></EnvironmentReadinessProvider>
}

function AppContent(): React.JSX.Element {
  const [state, setState] = useState<AppState | null>(null)
  const [page, setPage] = useState<AppPage>('dashboard')
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [outputTaskId, setOutputTaskId] = useState<string | null>(null)

  useEffect(() => {
    let mounted = true
    void window.appApi.getState().then((loaded) => {
      if (!mounted) return
      setState(loaded)
      setPage(loaded.settings.defaultPage)
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)))
    const unsubscribe = window.appApi.onStateChanged((next) => setState(next))
    return () => { mounted = false; unsubscribe() }
  }, [])

  useEffect(() => {
    if (!state) return
    const media = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: dark)') : null
    const update = (): void => {
      document.documentElement.dataset.theme = state.settings.theme === 'system'
        ? (media?.matches ? 'dark' : 'light') : state.settings.theme
    }
    update()
    media?.addEventListener('change', update)
    return () => media?.removeEventListener('change', update)
  }, [state?.settings.theme])

  const selectedTask = useMemo(() => state?.tasks.find((task) => task.id === selectedTaskId) ?? null, [selectedTaskId, state])
  const openTask = (id: string): void => { setSelectedTaskId(id); setPage('tasks') }
  const navigate = (target: AppPage): void => { setPage(target); setSelectedTaskId(null); setOutputTaskId(null) }
  const perform = async (action: () => Promise<void>): Promise<void> => {
    setActionError(null)
    try { await action() } catch (reason) { setActionError(reason instanceof Error ? reason.message : String(reason)) }
  }

  if (error) return <div className="fatal-error"><h1>文章成片启动失败</h1><p>{error}</p></div>
  if (!state) return <div className="loading-screen"><LoaderCircle className="spin" /><span>正在加载本地状态…</span></div>

  const createTask = async (input: CreateTaskInput): Promise<void> => perform(async () => {
    const created = await window.appApi.createTask(input)
    setState((current) => current && !current.tasks.some((task) => task.id === created.id)
      ? { ...current, tasks: [created, ...current.tasks] } : current)
    if (state.settings.openDetailAfterCreate) openTask(created.id)
    else navigate('tasks')
  })
  const updateSettings = async (patch: Partial<AppSettings>): Promise<void> => perform(async () => {
    const settings = await window.appApi.updateSettings(patch)
    setState((current) => current ? { ...current, settings } : current)
  })
  const createSample = async (): Promise<void> => perform(async () => {
    const created = await window.appApi.createSampleTask({ sample: 'sample-v4.1' })
    setState(current => current && !current.tasks.some(task => task.id === created.id) ? { ...current, tasks: [created, ...current.tasks] } : current)
    openTask(created.id)
  })
  const updateOutputView = async (view: OutputView): Promise<void> => updateSettings({ outputView: view })

  return (
    <div className="app-shell">
      <Sidebar page={page} onNavigate={navigate} />
      <main className="main-content">
        {actionError && <div className="notice error" role="alert">操作未完成：{actionError}<button onClick={() => setActionError(null)}>关闭提示</button></div>}
        {state.lastError?.startsWith('本地保存失败') && <div className="notice error" role="status">{state.lastError}；请检查诊断信息，勿将未保存状态视为已落盘。</div>}
        {page === 'dashboard' && <DashboardPage state={state} onCreate={() => navigate('create')} onOpenTask={openTask} onOpenOutputs={() => navigate('outputs')} />}
        {page === 'create' && <CreateTaskPage onCreate={createTask} onCreateSample={createSample} />}
        {page === 'tasks' && (selectedTask ? <TaskDetailPage key={selectedTask.id} task={selectedTask} onBack={() => setSelectedTaskId(null)} onStart={() => selectedTask.source === 'real' ? window.appApi.startTask(selectedTask.id) : perform(() => window.appApi.startTask(selectedTask.id))} onStop={() => perform(() => window.appApi.stopTask(selectedTask.id))} onRetry={() => perform(() => window.appApi.retryTask(selectedTask.id))} onOutputs={() => { setOutputTaskId(selectedTask.id); setPage('outputs') }} /> : <TaskListPage tasks={state.tasks} onOpen={openTask} />)}
        {page === 'outputs' && <OutputsPage outputs={state.outputs} focusedTaskId={outputTaskId} view={state.settings.outputView} onView={(view) => void updateOutputView(view)} onOpenTask={openTask} />}
        {page === 'settings' && <SettingsPage settings={state.settings} onUpdate={updateSettings} />}
      </main>
    </div>
  )
}
