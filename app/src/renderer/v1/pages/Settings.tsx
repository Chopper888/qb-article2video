import { useEffect, useState } from 'react'
import type { ThirdPartyComponent } from '../../../shared/v1/licenses'
import type { CleanupPreview, DiskUsageReal } from '../../../shared/v1/tasks'
import type { AppInfo, V1Prefs } from '../../../shared/v1/types'
import { gb } from '../demo-data'
import { KeyEditor, type KeyEditorProps } from '../KeyEditor'

// 设置（原型 I）：密钥 / 通用 / 工作文件夹与清理 / 关于与诊断。

export type SettingsTab = 'keys' | 'general' | 'folders' | 'about'
const TABS: [SettingsTab, string][] = [['keys', '密钥'], ['general', '通用'], ['folders', '工作文件夹与清理'], ['about', '关于与诊断']]

export interface SettingsProps {
  tab: SettingsTab
  onTab(tab: SettingsTab): void
  keys: Omit<KeyEditorProps, 'variant'>
  prefs: V1Prefs
  info: AppInfo
  lightAvailable: boolean
  onPrefs(patch: Partial<V1Prefs>): void
  onChooseFolder(target: 'work' | 'output'): void
  onOpenLogs(): void
  onRecheckEnv(): void
  onToast(text: string): void
  diagnostics: string
}

export function SettingsPage(props: SettingsProps): React.JSX.Element {
  const { tab, onTab } = props
  return (
    <div className="page">
      <div className="tabs" role="tablist">
        <h1>设置</h1>
        {TABS.map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => onTab(id)}>{label}</button>)}
      </div>
      <div className="settings-body">
        {tab === 'keys' && <KeyEditor variant="settings" {...props.keys} />}
        {tab === 'general' && <General {...props} />}
        {tab === 'folders' && <Folders {...props} />}
        {tab === 'about' && <About {...props} />}
      </div>
    </div>
  )
}

function General({ prefs, onPrefs, lightAvailable }: SettingsProps): React.JSX.Element {
  const [cost, setCost] = useState(String(prefs.maxCostCny))
  const commit = (): void => {
    const value = Number(cost)
    if (Number.isFinite(value) && value > 0) onPrefs({ maxCostCny: value }); else setCost(String(prefs.maxCostCny))
  }
  return (
    <div className="box">
      <div className="box-row">
        <div className="grow"><div className="t">默认费用上限</div><div className="d">每篇文章最多花这么多，到了就停；已做的部分保留，提高上限后点"继续"接着做。开跑前还可以单独改。</div></div>
        <label className="field">¥<input className="text-input limit-input" type="number" min={1} value={cost} aria-label="默认费用上限" onChange={event => setCost(event.target.value)} onBlur={commit} onKeyDown={event => { if (event.key === 'Enter') commit() }} /></label>
      </div>
      {lightAvailable && (
        <div className="box-row">
          <div className="grow"><div className="t">外观</div><div className="d">深色或浅色</div></div>
          <div className="seg">
            <button aria-pressed={prefs.theme === 'dark'} onClick={() => onPrefs({ theme: 'dark' })}>深色</button>
            <button aria-pressed={prefs.theme === 'light'} onClick={() => onPrefs({ theme: 'light' })}>浅色</button>
          </div>
        </div>
      )}
    </div>
  )
}

// 大小：不到 1 GB 时按 MB 显示（确认框里每篇几百 MB）
export function sizeText(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`
  return `${Math.max(0, Math.round(bytes / 1e3))} KB`
}

function Folders({ prefs, info, onChooseFolder, onToast }: SettingsProps): React.JSX.Element {
  const [disk, setDisk] = useState<DiskUsageReal | null>(null)
  const [preview, setPreview] = useState<CleanupPreview | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [working, setWorking] = useState(false)
  const [result, setResult] = useState<{ text: string; ok: boolean } | null>(null)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => { let alive = true; void window.a2v.diskUsage().then(value => { if (alive) setDisk(value) }, () => undefined); return () => { alive = false } }, [prefs.workDir, refresh])
  const total = disk?.total ?? 0
  const pct = (value: number): string => total > 0 ? `${(value / total) * 100}%` : '0%'
  const bin = info.platform === 'darwin' ? '废纸篓' : '回收站'
  const showPreview = (): void => {
    setResult(null)
    void window.a2v.cleanupPreview().then(value => { setPreview(value); setConfirming(value.articles.length > 0) }, (reason: unknown) => onToast(reason instanceof Error ? reason.message : String(reason)))
  }
  const run = (): void => {
    if (!preview) return
    setWorking(true)
    void window.a2v.cleanupRun(preview.articles.map(item => item.article)).then(outcome => {
      setResult(outcome.ok
        ? { ok: true, text: `已把 ${outcome.articles.length} 篇文章的中间文件移到${bin}，共 ${sizeText(outcome.bytes)}。` }
        : { ok: false, text: outcome.error ?? '清理没完成' })
    }, (reason: unknown) => setResult({ ok: false, text: reason instanceof Error ? reason.message : String(reason) }))
      .finally(() => { setWorking(false); setConfirming(false); setPreview(null); setRefresh(n => n + 1) })
  }
  return (
    <>
      <div className="box">
        <div className="box-row">
          <div className="grow"><div className="t">工作文件夹</div><div className="d">每篇文章的中间文件放在这里。路径越短越好（Windows 对路径长度有限制）。</div></div>
          <span className="v mono">{prefs.workDir}</span><button className="btn btn-sm" onClick={() => onChooseFolder('work')}>更改</button>
        </div>
        <div className="box-row">
          <div className="grow"><div className="t">成片保存位置</div></div>
          <span className="v mono">{prefs.outputDir}</span><button className="btn btn-sm" onClick={() => onChooseFolder('output')}>更改</button>
        </div>
      </div>
      <div className="box">
        <div className="disk">
          <div style={{ display: 'flex', justifyContent: 'space-between' }}><b>磁盘占用</b><span className="muted">{disk ? `工作文件夹共 ${gb(total)}` : '正在统计…'}</span></div>
          <div className="disk-bar" aria-hidden="true">
            <span style={{ width: pct(disk?.completed ?? 0), background: 'var(--accent)' }} />
            <span style={{ width: pct(disk?.unfinished ?? 0), background: 'var(--info)' }} />
            <span style={{ width: pct(disk?.cache ?? 0), background: 'var(--faint)' }} />
          </div>
          <div className="disk-legend">
            <div><span className="sw" style={{ background: 'var(--accent)' }} />已完成任务的中间文件 <b>{disk ? gb(disk.completed) : '—'}</b><div className="d muted">成片已另存，可以清理</div></div>
            <div><span className="sw" style={{ background: 'var(--info)' }} />未完成任务 <b>{disk ? gb(disk.unfinished) : '—'}</b><div className="d muted">清理后就不能"继续"了</div></div>
            <div><span className="sw" style={{ background: 'var(--faint)' }} />缓存 <b>{disk ? gb(disk.cache) : '—'}</b><div className="d muted">重做同一篇时可省钱</div></div>
          </div>
          <div className="field">
            <button className="btn btn-primary btn-lg" disabled={working} onClick={showPreview}>{working ? '正在清理…' : '清理已完成任务的中间文件'}</button>
            <span className="muted" style={{ fontSize: 12.5 }}>只清理已完成、成片已在成片保存位置里的文章；原文、成片、检查报告、日志和缓存保留，未完成和正在出片的不动。统计不含引擎自己的临时目录 _a2v_tmp。</span>
          </div>
          {preview && !preview.articles.length && <div className="muted" role="status">没有可以清理的已完成任务。</div>}
          {result && <div className={result.ok ? 'tone-ok' : 'tone-danger'} role="status">{result.text}</div>}
        </div>
      </div>
      {confirming && preview && (
        <div className="overlay">
          <div className="dialog" role="dialog" aria-label="清理中间文件">
            <h2>清理 {preview.articles.length} 篇已完成文章的中间文件</h2>
            <div className="cleanup-list" aria-label="要清理的文章">
              {preview.articles.map(item => <div className="row" key={item.article}><span title={item.article}>{item.title ?? item.article}</span><span>{sizeText(item.bytes)}</span></div>)}
              <div className="row"><b>合计</b><b>{sizeText(preview.totalBytes)}</b></div>
            </div>
            <p>会移到{bin}，可以找回。原文、成片、检查报告、日志和缓存保留。<br />再跑这篇时，引擎会按缓存重建，正常情况下不花钱或很少花钱。</p>
            <div className="acts">
              <button className="btn btn-lg" disabled={working} onClick={() => { setConfirming(false); setPreview(null) }}>取消</button>
              <button className="btn btn-primary btn-lg" disabled={working} onClick={run}>{working ? '正在移到' + bin + '…' : `移到${bin}`}</button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function About({ info, onRecheckEnv, onOpenLogs, onToast }: SettingsProps): React.JSX.Element {
  const [result, setResult] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  // 开发截图：开发者场景 export-diag 发这个事件
  useEffect(() => {
    const onDev = (): void => exportZip()
    window.addEventListener('a2v-dev-export', onDev)
    return () => window.removeEventListener('a2v-dev-export', onDev)
  })
  const exportZip = (): void => {
    setExporting(true)
    void window.a2v.exportDiagnostics().then(outcome => {
      if (outcome.ok) setResult(`已导出：${outcome.file}（${outcome.entries.length} 个文件，不含密钥）`)
      else if (!outcome.canceled) { setResult(null); onToast(outcome.error ?? '导出失败') }
    }, (reason: unknown) => onToast(reason instanceof Error ? reason.message : String(reason))).finally(() => setExporting(false))
  }
  const ready = info.engine.status === 'ready'
  return (
    <div className="box">
      <div className="box-row"><div className="grow"><div className="t">版本</div></div><span className="v">文章成片 {info.appVersion} · 引擎 {info.engine.version ?? '未知版本'}</span></div>
      <div className="box-row">
        <div className="grow"><div className="t">运行环境</div><div className="d">{ready ? `引擎目录、Python 都找得到` : info.engine.problems.join('；')}</div></div>
        <span className={ready ? 'tone-ok' : 'tone-danger'}><span className={ready ? 'dot' : 'dot danger'} /> {ready ? '正常' : '有问题'}</span>
        <button className="btn btn-sm" onClick={onRecheckEnv}>重新检查</button>
      </div>
      <div className="box-row" style={{ flexWrap: 'wrap' }}>
        <div className="grow"><div className="t">遇到问题时</div><div className="d">把诊断信息发给开发者排查（不含密钥）</div></div>
        <button className="btn btn-sm" disabled={exporting} onClick={exportZip}>{exporting ? '正在导出…' : '导出诊断信息'}</button>
        <button className="btn btn-sm" onClick={onOpenLogs}>打开日志文件夹</button>
        {result && <div style={{ width: '100%' }} className="tone-ok" role="status">{result}</div>}
      </div>
      {/* APP4：窗口顶部的菜单去掉了，原来"视图"菜单里有用的放这里 */}
      <div className="box-row">
        <div className="grow"><div className="t">界面卡住或显示不对时</div><div className="d">重新加载界面；正在出片的不受影响</div></div>
        <button className="btn btn-sm" onClick={() => void window.a2v.reloadWindow().catch((reason: unknown) => onToast(reason instanceof Error ? reason.message : String(reason)))}>重新加载界面</button>
        {info.devTools && <button className="btn btn-sm" onClick={() => void window.a2v.openDevTools().catch((reason: unknown) => onToast(reason instanceof Error ? reason.message : String(reason)))}>开发者工具</button>}
      </div>
      <ThirdParty onToast={onToast} />
    </div>
  )
}

// APP3：第三方组件（名称、版本、许可；点名称打开随包附带的许可文件）
function ThirdParty({ onToast }: { onToast(text: string): void }): React.JSX.Element {
  const [items, setItems] = useState<ThirdPartyComponent[] | null>(null)
  useEffect(() => { let alive = true; void window.a2v.listLicenses().then(list => { if (alive) setItems(list) }, () => { if (alive) setItems([]) }); return () => { alive = false } }, [])
  const fail = (reason: unknown): void => onToast(reason instanceof Error ? reason.message : String(reason))
  return (
    <div className="box-row third-party" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
      <div className="t">第三方组件</div>
      <table className="license-table">
        <thead><tr><th>名称</th><th>版本</th><th>许可</th></tr></thead>
        <tbody>
          {items?.map(item => (
            <tr key={item.id}>
              <td>{item.available ? <button className="link" onClick={() => void window.a2v.openLicense(item.id).catch(fail)}>{item.name}</button> : item.name}</td>
              <td className="mono">{item.version}</td>
              <td>{item.license}{item.officialLink && <> · <button className="link" onClick={() => void window.a2v.openExternal(item.officialLink!).catch(fail)}>官方许可</button></>}</td>
            </tr>
          ))}
          {!items && <tr><td colSpan={3} className="muted">正在读取…</td></tr>}
        </tbody>
      </table>
    </div>
  )
}
