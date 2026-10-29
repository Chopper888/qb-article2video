import { Grid2X2, List, LockKeyhole, Video } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { TaskOutput, OutputView } from '../../shared/types'
import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'

interface Props { outputs: TaskOutput[]; focusedTaskId?: string | null; view: OutputView; onView: (view: OutputView) => void; onOpenTask: (id: string) => void }

export function OutputsPage({ outputs, focusedTaskId, view, onView, onOpenTask }: Props): React.JSX.Element {
  const [detailId, setDetailId] = useState<string | null>(null)
  useEffect(() => {
    if (focusedTaskId) setDetailId(outputs.find((output) => output.taskId === focusedTaskId)?.id ?? null)
  }, [focusedTaskId, outputs])
  const selected = outputs.find((output) => output.id === detailId)
  return (
    <section>
      <PageHeader title="输出列表" description="Mock 为模拟结果；Real 展示主体提供的真实输出路径。" action={
        <div className="segmented">
          <button className={view === 'list' ? 'active' : ''} onClick={() => onView('list')} aria-label="列表视图"><List size={16} /> 列表视图</button>
          <button className={view === 'grid' ? 'active' : ''} onClick={() => onView('grid')} aria-label="缩略图视图"><Grid2X2 size={16} /> 缩略图视图</button>
        </div>
      } />
      {selected && <div className="section-card" aria-label={'mock' in selected ? '模拟输出详情' : 'Real 输出路径记录'}>
        <h2>{'mock' in selected ? '模拟输出详情' : 'Real 输出路径记录'} · {selected.title}</h2>
        {'mock' in selected ? <><p>{selected.status === 'quality_issue' ? '已经生成结果，但检查发现问题' : 'Mock 已完成'} · {selected.durationLabel} · {selected.resolution}</p><p>全部为模拟元数据，没有生成真实视频或文件夹。</p></> : <><p>{selected.deliveryPath}</p><p>展示主体提供的路径；未在 UI 读取或验证视频。时长与分辨率尚未提供。</p></>}
        <div className="action-row"><button className="text-button" onClick={() => onOpenTask(selected.taskId)}>回到对应任务</button><button disabled className="disabled-button">打开文件夹（{'mock' in selected ? 'Mock 不可用' : '当前不可用'}）</button><button className="text-button" onClick={() => setDetailId(null)}>关闭详情</button></div>
      </div>}
      {outputs.length ? view === 'grid' ? (
        <div className="output-grid">{outputs.map((output, index) => <OutputCard key={output.id} output={output} tone={index % 4} onOpen={() => setDetailId(output.id)} />)}</div>
      ) : (
        <div className="section-card output-list">
          <div className="output-row output-head"><span>标题</span><span>状态</span><span>时长</span><span>分辨率</span><span>生成时间</span><span>操作</span></div>
          {outputs.map((output) => <div className="output-row" key={output.id}><span><strong>{output.title}</strong><small className="muted-line"> · {'mock' in output ? '模拟输出' : 'Real 路径记录'}</small></span><span>{output.status === 'quality_issue' ? '出片有问题' : '已完成'}</span><span>{'mock' in output ? output.durationLabel : '未提供'}</span><span>{'mock' in output ? output.resolution : '未提供'}</span><span>{formatDate(output.createdAt)}</span><button className="text-button" onClick={() => setDetailId(output.id)}>{'mock' in output ? '查看模拟详情' : '查看路径记录'}</button></div>)}
        </div>
      ) : <div className="section-card"><EmptyState title="还没有输出记录" description="正常完成或“出片有问题”的任务输出会出现在这里。" /></div>}
    </section>
  )
}

function OutputCard({ output, tone, onOpen }: { output: TaskOutput; tone: number; onOpen: () => void }): React.JSX.Element {
  return <article className="output-card"><div className={`output-thumb output-tone-${tone}`}><Video size={34} /><span>{'mock' in output ? '模拟输出' : 'Real 路径记录'}</span><small>{'mock' in output ? output.durationLabel : '未读取视频'}</small></div><h3>{output.title}</h3><p>{output.status === 'quality_issue' ? '出片有问题' : '已完成'} · {'mock' in output ? output.resolution : '分辨率未提供'} · {formatDate(output.createdAt)}</p><div className="row-between"><button className="text-button" onClick={onOpen}>{'mock' in output ? '查看模拟详情' : '查看路径记录'}</button><button className="disabled-button" disabled title="本轮不打开文件"><LockKeyhole size={14} /> 打开文件夹</button></div></article>
}
function formatDate(value: string): string { return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
