import { FileText, FlaskConical, Link2, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import type { CreateTaskInput, MockScenario } from '../../shared/types'
import { PageHeader } from '../components/PageHeader'

const SCENARIOS: Array<{ value: MockScenario; title: string; description: string }> = [
  { value: 'success', title: '正常完成', description: '完整推进并产生模拟输出' },
  { value: 'quality_issue', title: '出片有问题', description: '生成结果，但模拟检查发现问题' },
  { value: 'failure', title: '执行失败', description: '在中途产生明确失败原因' }
]

export function CreateTaskPage({ onCreate, onCreateSample }: { onCreate: (input: CreateTaskInput) => Promise<void>; onCreateSample: () => Promise<void> }): React.JSX.Element {
  const [title, setTitle] = useState('我的模拟视频任务')
  const [scenario, setScenario] = useState<MockScenario>('success')
  const [submitting, setSubmitting] = useState(false)

  const submit = async (event: React.FormEvent): Promise<void> => {
    event.preventDefault()
    setSubmitting(true)
    try { await onCreate({ title, scenario }) } finally { setSubmitting(false) }
  }

  return (
    <section>
      <PageHeader title="新建任务" description="Mock 模拟与受控真实测试样本；创建任务不会自动开始生成。" />
      <form onSubmit={submit} className="create-layout">
        <div className="section-card">
          <div className="step-heading"><span>1</span><div><h2>选择文章来源</h2><p>仅开放测试样本，不开放新文章生产。</p></div></div>
          <div className="source-grid">
            <button type="button" className="source-card disabled" disabled>
              <FileText /><strong>本地 Markdown</strong><small>当前 APP 未开放本地 Markdown 入口</small>
            </button>
            <button type="button" className="source-card disabled" disabled>
              <Link2 /><strong>飞书文章</strong><small>当前 APP 未开放飞书文章入口</small>
            </button>
            <div className="source-card selected"><FlaskConical /><strong>测试样本</strong><small>Mock 模拟 / Real 样本</small></div>
          </div>
        </div>

        <div className="section-card">
          <div className="step-heading"><span>2</span><div><h2>任务信息</h2><p>创建与开始分开，创建不会自动运行。</p></div></div>
          <label className="field"><span>任务标题</span><input maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
          <div className="scenario-grid">
            {SCENARIOS.map((item) => (
              <label key={item.value} className={scenario === item.value ? 'scenario-card selected' : 'scenario-card'}>
                <input type="radio" name="scenario" value={item.value} checked={scenario === item.value} onChange={() => setScenario(item.value)} />
                <strong>{item.title}</strong><small>{item.description}</small>
              </label>
            ))}
          </div>
        </div>

        <div className="section-card readiness-card">
          <ShieldCheck size={24} /><div><strong>运行前检查</strong><p>Mock Engine 已就绪 · 不读取 API Key · 不运行真实渲染</p></div>
          <button className="primary-button" type="submit" disabled={submitting}>{submitting ? '创建中…' : '创建模拟任务'}</button>
        </div>
      </form>
      <div className="section-card">
        <h2>Real 样本：示例篇</h2><p>Article2Video v5 / --no-paid / 受控测试样本（Real，非 Mock）</p>
        <p>点击开始后才准备样本与运行时，真实运行主体并渲染视频，业务 API 费用为 0；可能耗时较久。</p>
        <p>Real Stop 与运行中关闭保护已启用；Retry / Resume 仍未实现。仅创建任务不会执行 Python。</p>
        <button className="secondary-button" disabled={submitting} onClick={() => {
          setSubmitting(true)
          void onCreateSample().finally(() => setSubmitting(false))
        }}>创建 Real 样本任务</button>
      </div>
    </section>
  )
}
