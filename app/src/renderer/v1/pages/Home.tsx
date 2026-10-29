import { Link2 } from 'lucide-react'
import { useState } from 'react'
import { TASK_TONE } from '../../../shared/v1/tasks'
import type { ImportSourceKind } from '../../../shared/v1/types'
import { timeText, useTasks } from './Lists'

export interface HomeProps {
  // APP3：运行环境还没准备好——"预估费用"不能点（导入可以先做：按钮改成"先导入"，准备好后自动估算）
  runtimeBusy?: boolean
  runtimeNotice?: React.ReactNode
  keysConfigured: number
  keysTotal: number
  busy: boolean
  onStart(input: string): void
  // APP4：导入本地文章。platform 是 darwin 时一个选择框能选文件夹、.zip、.md（kind='any'）；
  // 其他系统一个选择框不能同时选文件和文件夹，点链接后给两个选项
  platform: string
  onChooseSource(kind: ImportSourceKind): void
  // 一次拖进多个文件（一个 md 加图片）
  onDropFiles(paths: string[]): void
  onOpenKeys(): void
  onAllTasks(): void
}

// 拖进窗口的东西 → 本机路径（文件夹、.zip、单个 .md，或一个 md 加若干图片）
export function droppedPaths(event: React.DragEvent): string[] {
  const out: string[] = []
  for (const file of Array.from(event.dataTransfer.files ?? [])) {
    try { const path = window.a2v.pathForFile(file); if (path) out.push(path) } catch { /* 拿不到路径的跳过 */ }
  }
  return out
}

export function Home({ keysConfigured, keysTotal, busy, onStart, platform, onChooseSource, onDropFiles, onOpenKeys, onAllTasks, runtimeBusy = false, runtimeNotice }: HomeProps): React.JSX.Element {
  const [input, setInput] = useState('')
  const [dragging, setDragging] = useState(false)
  const [picking, setPicking] = useState(false)
  const choose = (): void => { if (platform === 'darwin') onChooseSource('any'); else setPicking(open => !open) }
  const { rows } = useTasks()
  const complete = keysConfigured === keysTotal
  const start = (): void => { if (input.trim()) onStart(input.trim()) }
  return (
    <div className="home" onDragOver={event => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)}
      onDrop={event => {
        event.preventDefault(); setDragging(false)
        const paths = droppedPaths(event)
        if (paths.length === 1) onStart(paths[0]!); else if (paths.length > 1) onDropFiles(paths)
      }}>
      <div>
        <h1>一篇文章，一键成片</h1>
        <p className="lead">粘贴飞书链接，或把文章文件夹、zip、md（可以连同图片一起）拖进来</p>
      </div>
      <div className={dragging ? 'home-col drop-hint' : 'home-col'}>
        {runtimeNotice}
        <label className="big-input">
          <Link2 size={18} aria-hidden="true" />
          <span className="sr-only">文章链接</span>
          <input type="text" value={input} placeholder="https://xxx.feishu.cn/wiki/…（飞书文章链接）" onChange={event => setInput(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter') start() }} />
          <button className="btn btn-primary btn-lg" disabled={busy || !input.trim()} onClick={start}>{busy ? '正在读取…' : runtimeBusy ? '先导入 →' : '预估费用 →'}</button>
        </label>
        <div className="home-meta">
          <span className="import-local">或 <button className="link" aria-expanded={platform === 'darwin' ? undefined : picking} onClick={choose}>导入本地文章</button>
            {picking && platform !== 'darwin' && (
              <span className="import-menu" role="menu">
                <button className="chip" role="menuitem" onClick={() => { setPicking(false); onChooseSource('folder') }}>选文件夹…</button>
                <button className="chip" role="menuitem" onClick={() => { setPicking(false); onChooseSource('file') }}>选 zip 或 md 文件…</button>
              </span>
            )}
          </span>
          <button className={`key-pill ${complete ? 'tone-ok' : 'tone-warn'}`} onClick={onOpenKeys}>
            <span className={complete ? 'dot' : 'dot warn'} />密钥 {keysConfigured}/{keysTotal} 已配置{complete ? '' : '，去补上'}
          </button>
        </div>
      </div>
      <div className="recent">
        <div className="recent-head"><span>最近任务</span><button className="link" onClick={onAllTasks}>全部任务</button></div>
        {(rows ?? []).slice(0, 3).map(task => (
          <div className="recent-row" key={task.article}>
            <span className="title">{task.title ?? task.article}</span>
            <span className={`state ${TASK_TONE[task.state]}`}>{task.stateText}</span>
            <span className="time">{timeText(task.updatedAt)}</span>
          </div>
        ))}
        {rows && !rows.length && <div className="recent-row"><span className="title muted">还没有任务</span></div>}
      </div>
    </div>
  )
}
