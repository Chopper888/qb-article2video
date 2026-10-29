import type { RealTask } from '../../shared/types'
import { useId, useState } from 'react'
import { LEGACY_RUNNING_MESSAGE } from '../../shared/real-task'
import { StatusBadge } from '../components/StatusBadge'
import { EnvironmentReadinessPanel, useEnvironmentReadiness } from '../components/EnvironmentReadiness'

export function RealTaskDetail({ task, onBack, onStart, onStop, onOutputs }: { task: RealTask; onBack: () => void; onStart: () => Promise<void>; onStop: () => Promise<void>; onOutputs: () => void }): React.JSX.Element {
  const [busy, setBusy] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [logsExpanded, setLogsExpanded] = useState(false)
  const logsId = useId()
  const data = task.real
  const processed = data.steps.filter((step) => step.status === 'done' || step.status === 'skipped').length
  const legacyRunning = task.status === 'running' && task.error === LEGACY_RUNNING_MESSAGE
  const startupFailure = task.status === 'failed' && data.runDone === null && data.missingRunDone === true
  const engineVersion = task.sample === 'sample-v4.1'
    ? (data.association.engineCommit === '7ec8ae28b54c6bd52e43c4697a83c15304317d31' ? 'v4.1'
      : data.association.engineCommit === '9b8a435b1b603753de32d49170298f703d60fe72' ? 'v4.1.1' : data.association.engineCommit === 'c949c8bff22e5d5bb365468dda256f19f35e6433' ? 'v5' : 'v4.1.2')
    : 'v3.2'
  const historicalPending = task.sample === 'sample-v3.2' && task.status === 'pending'
  const canCheckStart = task.sample === 'sample-v4.1' && task.status === 'pending'
  const readiness = useEnvironmentReadiness(canCheckStart)
  const environmentReady = !readiness.loading && readiness.result?.status === 'ready'
  const start = async (): Promise<void> => {
    if (busy || !environmentReady) return
    setBusy(true)
    setStartError(null)
    try { await onStart() } catch (reason) {
      // Electron invoke errors can include an IPC wrapper and a multiline stack.
      const firstLine = (reason instanceof Error ? reason.message : typeof reason === 'string' ? reason : '').split(/\r?\n/)[0] ?? ''
      const message = firstLine.replace(/^Error invoking remote method '[^']+':\s*/, '').replace(/^Error:\s*/, '').trim()
      setStartError(message.slice(0, 300) || '无法开始生成，请重新检查运行环境后重试。')
      void readiness.refresh()
    } finally { setBusy(false) }
  }
  const stop = async (): Promise<void> => {
    if (!window.confirm('确定停止当前真实任务吗？已生成的任务文件会保留。')) return
    setBusy(true)
    try { await onStop() } finally { setBusy(false) }
  }
  return <section>
    <button className="back-button" onClick={onBack}>返回任务列表</button>
    <header className="detail-header"><div><h1>{task.title}</h1><StatusBadge status={task.status} /></div>
      <div className="action-row">
        {task.sample ? task.status === 'pending' && (historicalPending
          ? <button disabled className="disabled-button">历史 v3.2 任务不可启动</button>
          : <button className={busy || !environmentReady ? 'disabled-button' : 'primary-button'} disabled={busy || !environmentReady} onClick={() => void start()}>开始生成</button>)
          : <button disabled className="disabled-button">开始（当前不可用）</button>}
        <button disabled={busy || task.status !== 'running'} className={task.status === 'running' ? 'danger-button' : 'disabled-button'} onClick={() => void stop()}>{busy && task.status === 'running' ? '正在停止…' : legacyRunning ? '停止遗留任务' : '停止任务'}</button>
        {data.deliveryPath && (task.status === 'completed' || task.status === 'quality_issue') && <button className="secondary-button" onClick={onOutputs}>打开对应输出</button>}
      </div>
    </header>
    {startError && <div className="notice error" role="alert">未能开始生成：{startError}</div>}
    {canCheckStart && <EnvironmentReadinessPanel {...readiness} />}
    <div className="notice neutral">{task.sample ? `Real 引擎 · 示例篇 · Article2Video ${engineVersion} · --no-paid。会真实调用主体并渲染视频，业务 API 费用为 0；Stop 已可用，Retry / Resume 尚未实现。运行中关闭 APP 时会先要求停止任务。` : '当前 Real 仅开放示例篇样本。此记录不能执行。'}</div>
    {historicalPending && <div className="notice neutral">这是历史 v3.2 联调记录；当前 APP 新任务使用 v5。</div>}
    {legacyRunning && <div className="notice warning"><strong>{LEGACY_RUNNING_MESSAGE}</strong><span>已安全核实仍是原任务进程；可停止，但不会自动续跑。</span></div>}
    {task.status === 'completed' && <div className="notice neutral"><strong>主体运行完成</strong></div>}
    {task.status === 'quality_issue' && <div className="notice warning"><strong>视频已产出，但质量检查未通过。</strong><span>已保留正式输出路径与原始退出事实。</span></div>}
    {startupFailure && <div className="notice error"><strong>主体未正常完成。</strong><span>未收到正式 run_done；请结合退出事实和 stderr 诊断尾部排查。</span></div>}
    {task.status === 'stopped' && <div className="notice neutral"><strong>任务已停止</strong><span>已保留现有步骤、日志、路径和退出事实。</span></div>}
    {task.status === 'interrupted' && <div className="notice warning"><strong>任务已中断</strong><span>{interruptionMessage(task)}</span></div>}
    {task.error && task.status === 'failed' && !legacyRunning && <div className="notice error">{task.error}</div>}
    {task.preparation && <div className="section-card"><h2>APP 前置阶段：准备真实样本与运行时</h2>
      <p>{task.preparation.status}（不是主体步骤）</p><p>{task.preparation.taskRoot}</p>
      {task.preparation.stdoutTruncated && <p className="muted-line">仅保留最近的准备 stdout 诊断内容</p>}
      <pre>{task.preparation.stdout}</pre><h3>准备 stderr</h3>
      {task.preparation.stderrTruncated && <p className="muted-line">仅保留最近的 stderr 诊断内容</p>}
      <pre>{task.preparation.stderr || '无'}</pre>
      <p>准备进程退出码：{task.preparation.processExit?.code ?? '尚未结束'}</p>
    </div>}
    <div className="section-card">
      <h2>动态步骤</h2><p>已处理 {processed} / 已知步骤 {data.steps.length}（不代表总工作量，无 ETA）</p>
      {data.steps.length ? <div className="step-strip">{data.steps.map((step) => <div key={step.step} className={`step ${step.status === 'done' || step.status === 'skipped' ? 'done' : ''}`}>
        <strong>{step.step}</strong><span>{step.name ?? '名称尚未提供'}</span><small>{step.status}</small><p>{step.message}</p>
      </div>)}</div> : <p>尚未收到正式步骤记录</p>}
    </div>
    <div className="section-card"><h2>输出路径记录</h2><p>{data.deliveryPath ?? '尚未提供真实输出'}</p><p>只展示来源路径；未读取、打开或验证视频文件。</p></div>
    <div className="section-card"><h2>原始退出事实</h2>
      <p>run_done：{data.runDone ? `${data.runDone.exitCode} · ${data.runDone.message}` : '尚未收到'}</p>
      <p>进程退出码：{data.processExit?.code ?? '尚未提供'}；信号：{data.processExit?.signal ?? '未提供'}</p>
      {data.processExit?.error && <p>{data.processExit.error}</p>}
      <p>缺少 run_done：{data.missingRunDone === null ? '尚未终结' : data.missingRunDone ? '是' : '否'}</p>
      {data.diagnostics.map((item, index) => <div key={index}><strong>{item.kind}</strong><p>{item.message}</p>{item.raw && <pre>{item.raw}</pre>}</div>)}
    </div>
    <div className="section-card"><h2>事件与日志</h2>
      <p>包含主体事件与诊断日志，需要排查时再展开。</p>
      <button className="secondary-button" aria-expanded={logsExpanded} aria-controls={logsId} onClick={() => setLogsExpanded((expanded) => !expanded)}>{logsExpanded ? '收起日志' : '展开日志'}</button>
      <div id={logsId} hidden={!logsExpanded}>{logsExpanded && <>
        {task.logs.map((log) => <p key={log.id}>{log.message}</p>)}
        <h3>stderr（独立文本）</h3>
        {data.stderrTruncated && <p className="muted-line">仅保留最近的 stderr 诊断内容</p>}
        <pre>{data.stderr || '尚未收到'}</pre>
      </>}</div>
    </div>
    <div className="section-card"><p>APP 任务 ID：{task.id}</p><p>APP 观察时间（非主体账本时间）：{task.startedAt ?? '未开始'} → {task.finishedAt ?? '未结束'}</p></div>
  </section>
}

const INTERRUPTION_MESSAGES: Record<NonNullable<RealTask['interruptionReason']>, string> = {
  missing_pid: '缺少可核实的原进程信息，APP 将任务标记为已中断。',
  process_missing: '原任务进程已不存在，APP 将任务标记为已中断。',
  process_mismatch: '原 PID 当前属于其它或不匹配进程，APP 未执行终止。',
  process_identity_unknown: '无法确认原进程身份，APP 为避免误杀未执行终止。'
}

function interruptionMessage(task: RealTask): string {
  return task.interruptionReason ? INTERRUPTION_MESSAGES[task.interruptionReason] : 'APP 上次异常结束，未确认任务正常完成。'
}
