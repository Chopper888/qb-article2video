import { Activity, ChevronDown, Clipboard, Folder, KeyRound, MonitorCog, Settings2, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { AppSettings, Diagnostics, DirectorySettings } from '../../shared/types'
import { PageHeader } from '../components/PageHeader'
import { EnvironmentReadinessPanel, useEnvironmentReadiness } from '../components/EnvironmentReadiness'

type SettingsTab = 'general' | 'engine' | 'directories' | 'keys' | 'diagnostics'
const TABS: Array<{ id: SettingsTab; label: string }> = [
  { id: 'general', label: '通用' }, { id: 'engine', label: '引擎与环境' }, { id: 'directories', label: '目录' }, { id: 'keys', label: 'API Key' }, { id: 'diagnostics', label: '诊断与高级' }
]

export function SettingsPage({ settings, onUpdate }: { settings: AppSettings; onUpdate: (patch: Partial<AppSettings>) => Promise<void> }): React.JSX.Element {
  const [tab, setTab] = useState<SettingsTab>('general')
  const readiness = useEnvironmentReadiness()
  return <section><PageHeader title="设置" description="管理 APP 偏好、Real 引擎能力与诊断信息。" /><div className="settings-tabs">{TABS.map((item) => <button key={item.id} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)}>{item.label}</button>)}</div>
    <EnvironmentReadinessPanel {...readiness} />
    {tab === 'general' && <General settings={settings} onUpdate={onUpdate} />}
    {tab === 'engine' && <Engine />}
    {tab === 'directories' && <Directories directories={settings.directories} onUpdate={(directories) => onUpdate({ directories })} />}
    {tab === 'keys' && <ApiKeys />}
    {tab === 'diagnostics' && <DiagnosticsPanel />}
  </section>
}

function General({ settings, onUpdate }: { settings: AppSettings; onUpdate: (patch: Partial<AppSettings>) => Promise<void> }): React.JSX.Element {
  return <div className="settings-stack"><div className="section-card"><SettingTitle icon={<MonitorCog />} title="界面与显示" description="选择外观和默认页面。" />
    <SettingRow title="主题模式" description="浅色主题已完成，深色提供基础适配"><div className="segmented"><button className={settings.theme === 'system' ? 'active' : ''} onClick={() => void onUpdate({ theme: 'system' })}>跟随系统</button><button className={settings.theme === 'light' ? 'active' : ''} onClick={() => void onUpdate({ theme: 'light' })}>浅色</button><button className={settings.theme === 'dark' ? 'active' : ''} onClick={() => void onUpdate({ theme: 'dark' })}>深色</button></div></SettingRow>
    <SettingRow title="默认启动页" description="APP 启动后默认进入的页面"><select value={settings.defaultPage} onChange={(event) => void onUpdate({ defaultPage: event.target.value as AppSettings['defaultPage'] })}><option value="dashboard">工作台</option><option value="tasks">任务列表</option><option value="outputs">输出列表</option><option value="create">新建任务</option></select></SettingRow>
    <SettingRow title="输出列表默认视图" description="与输出页切换共用同一偏好"><div className="segmented"><button className={settings.outputView === 'list' ? 'active' : ''} onClick={() => void onUpdate({ outputView: 'list' })}>列表视图</button><button className={settings.outputView === 'grid' ? 'active' : ''} onClick={() => void onUpdate({ outputView: 'grid' })}>缩略图视图</button></div></SettingRow>
  </div><div className="section-card"><SettingTitle icon={<Settings2 />} title="基础行为" description="控制创建任务后的 APP 行为。" /><SettingRow title="创建任务后进入详情页" description="关闭时将回到任务列表"><label className="switch"><input type="checkbox" checked={settings.openDetailAfterCreate} onChange={(event) => void onUpdate({ openDetailAfterCreate: event.target.checked })} /><span /></label></SettingRow></div></div>
}

function Engine(): React.JSX.Element { return <div className="settings-stack"><div className="engine-banner"><ShieldCheck /><div><strong>Real 样本与能力</strong><p>当前仅开放示例篇样本，使用 Article2Video v5 与 --no-paid；不代表生产全功能已完成。</p></div></div><div className="section-card"><SettingTitle icon={<Activity />} title="引擎信息" description="当前 APP 已接入能力与限制。" /><div className="info-grid"><Info label="APP 模式" value="Mock 模拟测试 + Real 样本" /><Info label="Real Engine" value="Article2Video v5" /><Info label="当前样本" value="示例篇（sample-v4.1）" /><Info label="运行模式" value="--no-paid" /><Info label="Stop" value="可用" /><Info label="interrupted recovery" value="可用" /><Info label="Retry / Resume" value="未实现" /></div></div><div className="section-card"><SettingTitle icon={<ShieldCheck />} title="当前能力边界" description="只展示当前支持的样本能力，不代表生产全功能。" /><div className="check-list"><span>Mock：用于模拟测试</span><span>Real：支持示例篇样本，运行前需通过环境检查</span><span>Stop 与异常退出后的遗留任务识别可用</span><span>不自动续跑；Retry / Resume 尚未实现</span></div></div></div> }

function Directories({ directories, onUpdate }: { directories: DirectorySettings; onUpdate: (value: DirectorySettings) => Promise<void> }): React.JSX.Element {
  const [draft, setDraft] = useState(directories)
  useEffect(() => setDraft(directories), [directories])
  const rows: Array<{ key: keyof DirectorySettings; title: string; description: string }> = [
    { key: 'workspace', title: '工作目录显示值', description: '仅保存显示值，不创建主体目录' },
    { key: 'output', title: '输出目录显示值', description: '仅保存显示值；真实输出由示例篇联调任务记录' },
    { key: 'integration', title: '联调目录显示值', description: '仅保存显示值；当前联调范围为示例篇样本' },
    { key: 'logs', title: '日志目录显示值', description: '诊断文本保存在 APP 状态 JSON 中' }
  ]
  return <div className="settings-stack">{rows.map((row) => <div className="section-card directory-card" key={row.key}><span className="setting-icon"><Folder /></span><div className="grow"><h3>{row.title}</h3><p>{row.description}</p><input value={draft[row.key]} placeholder="未配置" onChange={(event) => setDraft({ ...draft, [row.key]: event.target.value })} onBlur={() => void onUpdate(draft)} /></div></div>)}</div>
}

function ApiKeys(): React.JSX.Element {
  const services = ['DeepSeek', '豆包 / 火山引擎', 'RunningHub', '方舟']
  return <div className="section-card"><SettingTitle icon={<KeyRound />} title="API Key" description="仅保留 UI，占位值不会保存或发起连接测试。" /><div className="api-note">当前示例篇样本固定使用 --no-paid，不读取或保存真实 Key。</div>{services.map((service) => <div className="key-row" key={service}><strong>{service}</strong><input type="password" value="" readOnly placeholder="--no-paid 模式不配置" /><span>未启用</span></div>)}</div>
}

function DiagnosticsPanel(): React.JSX.Element {
  const [data, setData] = useState<Diagnostics | null>(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const report = (reason: unknown): void => setError(reason instanceof Error ? reason.message : String(reason))
  useEffect(() => { void window.appApi.getDiagnostics().then(setData).catch(report) }, [])
  const copy = async (): Promise<void> => {
    if (!data) return
    try { await navigator.clipboard.writeText(JSON.stringify(data, null, 2)); setCopied(true) } catch (reason) { report(reason) }
  }
  if (error) return <div className="notice error" role="alert">诊断操作失败：{error}</div>
  return <div className="settings-stack"><div className="section-card"><div className="row-between"><SettingTitle icon={<Activity />} title="诊断信息" description="用于核对 APP、Mock 与 Real 引擎状态。" /><button className="secondary-button" onClick={() => void copy()}><Clipboard size={15} /> {copied ? '已复制' : '复制诊断信息'}</button></div>{data ? <div className="diagnostic-table"><Info label="APP 版本" value={data.appVersion} /><Info label="引擎模式" value="Mock + Real" /><Info label="Electron / Node" value={`${data.electronVersion} / ${data.nodeVersion}`} /><Info label="用户数据位置" value={data.userDataPath} /><Info label="当前主题" value={data.theme} /><Info label="当前任务数量" value={String(data.taskCount)} /><Info label="当前运行任务" value={data.runningTask ?? '无'} /><Info label="最近错误" value={data.lastError ?? '无'} /></div> : <p className="muted-line">正在读取诊断信息…</p>}</div><details className="section-card advanced"><summary>高级设置（后续扩展） <ChevronDown size={16} /></summary><p>当前不开放额外高级选项，仅支持当前样本范围。</p></details></div>
}

function SettingTitle({ icon, title, description }: { icon: React.ReactNode; title: string; description: string }): React.JSX.Element { return <div className="setting-title"><span className="setting-icon">{icon}</span><div><h2>{title}</h2><p>{description}</p></div></div> }
function SettingRow({ title, description, children }: { title: string; description: string; children: React.ReactNode }): React.JSX.Element { return <div className="setting-row"><span><strong>{title}</strong><small>{description}</small></span>{children}</div> }
function Info({ label, value }: { label: string; value: string }): React.JSX.Element { return <div className="info-item"><span>{label}</span><strong>{value}</strong></div> }
