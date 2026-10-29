import { Play } from 'lucide-react'
import { useEffect, useState } from 'react'
import { filterTasks, formatDuration, TASK_FILTERS, TASK_TONE, type TaskRow, type VideoItem } from '../../../shared/v1/tasks'

// 任务列表、成片库（原型 E）。APP2：真实数据——扫描工作文件夹、成片保存位置。读不到的字段显示"—"。

const dash = '—'

export function timeText(iso: string | null): string {
  if (!iso) return dash
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return dash
  const pad = (n: number): string => String(n).padStart(2, '0')
  const today = new Date()
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  if (date.toDateString() === today.toDateString()) return `今天 ${hm}`
  const yesterday = new Date(today.getTime() - 86_400_000)
  if (date.toDateString() === yesterday.toDateString()) return `昨天 ${hm}`
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${hm}`
}

export function useTasks(refreshKey: unknown = null): { rows: TaskRow[] | null; error: string | null } {
  const [rows, setRows] = useState<TaskRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    window.a2v.listTasks().then(list => { if (alive) setRows(list) }, (reason: unknown) => { if (alive) setError(reason instanceof Error ? reason.message : String(reason)) })
    return () => { alive = false }
  }, [refreshKey])
  return { rows, error }
}

const actionFor = (row: TaskRow): string => row.state === 'stopped' ? '继续' : row.state === 'failed' ? '处理' : '查看'
const typeText = (row: TaskRow): string => row.contentType === 'screenshot' ? '截图型' : row.contentType === 'theory' ? '理论型' : dash

// resumeBlocked：运行环境还没准备好（APP3），"继续""处理""重新出片"不能点
// APP4：已完成的（清理过中间文件的也算）多一个"重新出片"；workDir 变了立刻重新读
// 1.0.1（R101 J）：每个任务可以删除——确认框里写明要移到回收站的文件夹；正在出片的不能删
export function TasksPage({ onOpen, onRemake, resumeBlocked = false, workDir, platform, onToast }: { onOpen(row: TaskRow): void; onRemake?(row: TaskRow): void; resumeBlocked?: boolean; workDir?: string; platform?: string; onToast?(text: string): void }): React.JSX.Element {
  const [filter, setFilter] = useState('all')
  const [reload, setReload] = useState(0)
  const { rows, error } = useTasks(`${workDir ?? ''}#${reload}`)
  const shown = filterTasks(rows ?? [], filter)
  const [confirm, setConfirm] = useState<{ row: TaskRow; path: string } | null>(null)
  const [deleting, setDeleting] = useState(false)
  const bin = platform === 'darwin' ? '废纸篓' : '回收站'
  const say = (text: string): void => onToast?.(text)
  const askDelete = (row: TaskRow): void => {
    void window.a2v.deleteTaskCheck(row.article).then(check => {
      if (check.ok) setConfirm({ row, path: check.path }); else say(check.error)
    }, (reason: unknown) => say(reason instanceof Error ? reason.message : String(reason)))
  }
  const doDelete = (): void => {
    if (!confirm) return
    setDeleting(true)
    void window.a2v.deleteTask(confirm.row.article).then(outcome => {
      say(outcome.ok ? `已把「${confirm.row.title ?? confirm.row.article}」移到${bin}，可以从${bin}找回` : outcome.error)
    }, (reason: unknown) => say(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => { setDeleting(false); setConfirm(null); setReload(n => n + 1) })
  }
  return (
    <div className="page">
      <div className="page-head">
        <h1>任务</h1>
        <div className="chips">{TASK_FILTERS.map(item => <button key={item.id} className="chip" aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}>{item.label}</button>)}</div>
      </div>
      <div className="table">
        <table>
          <thead><tr><th style={{ width: '38%' }}>文章</th><th>来源</th><th>类型</th><th>状态</th><th>进度</th><th>费用</th><th>时间</th><th /></tr></thead>
          <tbody>
            {shown.map(task => (
              <tr key={task.article}>
                <td title={task.article}>{task.title ?? task.article}</td>
                <td className="muted">{task.source ?? dash}</td>
                <td className="muted">{typeText(task)}</td>
                <td className={TASK_TONE[task.state]}>{task.stateText}</td>
                <td className="muted">{task.stepDone === null || task.stepTotal === null ? dash : task.stepDone === task.stepTotal ? `${task.stepDone} / ${task.stepTotal}` : `第 ${task.stepDone} 步 / ${task.stepTotal}`}</td>
                <td className="muted">{task.costCny === null ? dash : `¥${task.costCny.toFixed(2)}`}</td>
                <td className="muted">{timeText(task.updatedAt)}</td>
                <td className="action">
                  <button className="link" disabled={resumeBlocked && (task.state === 'stopped' || task.state === 'failed')} onClick={() => onOpen(task)}>{actionFor(task)}</button>
                  {onRemake && (task.state === 'done' || task.state === 'done_red') && <> · <button className="link" disabled={resumeBlocked} onClick={() => onRemake(task)}>重新出片</button></>}
                  {' · '}<button className="link" disabled={task.state === 'running'} title={task.state === 'running' ? '正在出片，不能删除' : undefined} onClick={() => askDelete(task)}>删除</button>
                </td>
              </tr>
            ))}
            {rows && !shown.length && <tr><td colSpan={8} className="muted">没有这类任务。</td></tr>}
            {!rows && !error && <tr><td colSpan={8} className="muted">正在读取…</td></tr>}
            {error && <tr><td colSpan={8} className="tone-danger">读取任务失败：{error}</td></tr>}
          </tbody>
        </table>
      </div>
      {confirm && (
        <div className="overlay">
          <div className="dialog" role="dialog" aria-label="删除任务">
            <h2>删除任务「{confirm.row.title ?? confirm.row.article}」？</h2>
            <p>这篇文章的工作文件夹会整个移到{bin}（可以从{bin}找回），任务列表里不再显示：</p>
            <p className="mono" aria-label="要移到回收站的文件夹">{confirm.path}</p>
            <p>成片保存位置里已经交付的成片不受影响。</p>
            <div className="acts">
              <button className="btn btn-lg" disabled={deleting} onClick={() => setConfirm(null)}>取消</button>
              <button className="btn btn-danger btn-lg" disabled={deleting} onClick={doDelete}>{deleting ? `正在移到${bin}…` : `移到${bin}`}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export function LibraryPage({ outputDir, onOpenFolder, onPlay }: { outputDir: string; onOpenFolder(): void; onPlay(video: VideoItem): void }): React.JSX.Element {
  const [videos, setVideos] = useState<VideoItem[] | null>(null)
  useEffect(() => { let alive = true; void window.a2v.listVideos().then(list => { if (alive) setVideos(list) }, () => { if (alive) setVideos([]) }); return () => { alive = false } }, [outputDir])
  return (
    <div className="page">
      <div className="page-head">
        <h1>成片库</h1>
        <span className="muted" style={{ fontSize: 13 }}>保存位置：<span className="mono">{outputDir}</span> · <button className="link" onClick={onOpenFolder}>打开</button></span>
      </div>
      <div className="cards">
        {videos?.map(video => (
          <button className="card" key={video.file} onClick={() => onPlay(video)} style={{ textAlign: 'left', padding: 0, cursor: 'pointer' }}>
            <div className="thumb">{video.cover && <img src={video.cover} alt="" />}<span className="play"><Play size={15} fill="currentColor" /></span><span className="dur">{formatDuration(video.durationSec)}</span></div>
            <div className="meta">
              <span className="t" title={video.title}>{video.title}</span>
              <span className={`s ${video.reds ? 'tone-danger' : video.verdict ? 'tone-ok' : 'muted'}`}>{video.verdict === null && video.reds === null ? '检查结论：—' : video.reds ? `${video.reds} 处标红` : video.notices ? `检查通过 · ${video.notices} 条提示` : '检查通过'}</span>
            </div>
          </button>
        ))}
      </div>
      {videos && !videos.length && <p className="note">成片保存位置里还没有成片。</p>}
      {!videos && <p className="note">正在读取…</p>}
    </div>
  )
}
