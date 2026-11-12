import { useEffect, useState } from 'react'
import type { ThirdPartyComponent } from '../../../shared/v1/licenses'
import type { CleanupPreview, DiskUsageReal } from '../../../shared/v1/tasks'
import { DEFAULT_COMFYUI_URL, IMAGE_BACKENDS, MOSS_STYLES, type AppInfo, type ImageBackend, type MossStyle, type V1Prefs } from '../../../shared/v1/types'
import styleS0 from '../assets/moss-style-S0.jpg'
import styleS1 from '../assets/moss-style-S1.jpg'
import styleS3 from '../assets/moss-style-S3.jpg'
import styleS4 from '../assets/moss-style-S4.jpg'
import { gb } from '../demo-data'
import { KeyEditor, type KeyEditorProps } from '../KeyEditor'
import type { UpdateState } from '../../../shared/v1/auto-update'
import { UpdateAboutStatus, type UpdateActions } from '../UpdateBar'

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
  // R121M5：自动更新（状态来自 Main；不给时"检查更新"直接向 Main 问）
  update?: UpdateState | null
  updateActions?: UpdateActions
  onCheckUpdate?(): Promise<unknown>
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
      <ImageSettings prefs={prefs} onPrefs={onPrefs} />
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

// R120M P1：出图一组。出图方式为 GPT 时，风格、地址、补图三项置灰（只在 MOSS 时起作用）。
// R120M3：第三种出图方式 MOSS · RH 云端。风格、补图两种 MOSS 都用；ComfyUI 地址只对 MOSS 本地有效，其余情况置灰。
// R121M：出图方式、风格改为下拉框（风格下拉框旁显示该风格的示例小图）；删掉"RH 同时出图数"（引擎自动调节）。
const BACKEND_TEXT: Record<ImageBackend, string> = {
  gpt: 'GPT（RunningHub）：在 RunningHub 上出图，按张付费，不需要显卡，几张同时出、比较快。',
  moss_local: 'MOSS 本地：用你电脑上的 ComfyUI 出图，不花钱，但需要较好的显卡（建议 24 GB 显存），每张约 1 分钟。',
  moss_rh: 'MOSS · RH 云端：用 RunningHub 上的 MOSS 应用出图，消耗 RH 币（每张约 70 币），不需要本地显卡；同时出几张由软件自动调节。'
}
// 示例小图：Hermes 样片 v1 的出图（S0 / S3 / S4，G07「习惯」）与交付-v3 总览里的 S1 一格，缩到 320×180
const STYLE_SAMPLES: Record<MossStyle, string> = { S0: styleS0, S3: styleS3, S1: styleS1, S4: styleS4 }
const STYLE_TEXT: Record<MossStyle, string> = {
  S0: '手绘线稿：浅灰底、黑色线稿，文字最稳。',
  S3: '科技霓虹：深蓝紫底、青色发光线条。',
  S1: '日系水彩：淡雅水彩、暖色手写字。',
  S4: '扁平矢量：米色底、深蓝和橙色的扁平插画。'
}

function ImageSettings({ prefs, onPrefs }: Pick<SettingsProps, 'prefs' | 'onPrefs'>): React.JSX.Element {
  const backend = prefs.imageBackend
  const moss = backend === 'moss_local' || backend === 'moss_rh'
  const local = backend === 'moss_local'
  const [url, setUrl] = useState(prefs.comfyuiUrl)
  useEffect(() => { setUrl(prefs.comfyuiUrl) }, [prefs.comfyuiUrl])
  const commitUrl = (): void => {
    const next = url.trim() || DEFAULT_COMFYUI_URL
    if (next !== prefs.comfyuiUrl) onPrefs({ comfyuiUrl: next })
    setUrl(next)
  }
  return (
    <div className="image-group" role="group" aria-label="出图">
      <div className="box-row group-head"><div className="grow"><div className="t">出图</div><div className="d">插画由谁来画。改了以后，已出的图按新设置重新出（先查缓存，出过的不重复花钱）。</div></div></div>
      <div className="box-row">
        <div className="grow"><div className="t">出图方式</div>
          <div className="d">{BACKEND_TEXT[backend] ?? BACKEND_TEXT.gpt}</div></div>
        <select className="select-input" aria-label="出图方式" value={backend}
          onChange={event => onPrefs({ imageBackend: event.target.value as ImageBackend })}>
          {IMAGE_BACKENDS.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </div>
      <div className={moss ? 'box-row' : 'box-row is-disabled'} aria-disabled={!moss}>
        <div className="grow"><div className="t">风格</div><div className="d">只对 MOSS 出图（本地和 RH 云端）有效。插画铺满整个画面，字幕压在图上。{STYLE_TEXT[prefs.mossStyle] ?? ''}</div></div>
        <img className="style-sample" src={STYLE_SAMPLES[prefs.mossStyle] ?? STYLE_SAMPLES.S0} alt={`${MOSS_STYLES.find(s => s.id === prefs.mossStyle)?.name ?? ''}示例`} width={128} height={72} />
        <select className="select-input" aria-label="MOSS 风格" disabled={!moss} value={prefs.mossStyle}
          onChange={event => onPrefs({ mossStyle: event.target.value as MossStyle })}>
          {MOSS_STYLES.map(style => <option key={style.id} value={style.id}>{style.name}</option>)}
        </select>
      </div>
      <div className={local ? 'box-row' : 'box-row is-disabled'} aria-disabled={!local}>
        <div className="grow"><div className="t">ComfyUI 地址</div><div className="d">只对 MOSS 本地有效。ComfyUI 启动后显示的网址，装在本机、没改过端口的不用动。</div></div>
        <label className="field"><input className="text-input" type="text" disabled={!local} placeholder={DEFAULT_COMFYUI_URL} value={url} aria-label="ComfyUI 地址"
          onChange={event => setUrl(event.target.value)} onBlur={commitUrl} onKeyDown={event => { if (event.key === 'Enter') commitUrl() }} /></label>
      </div>
      <div className={moss ? 'box-row' : 'box-row is-disabled'} aria-disabled={!moss}>
        <div className="grow"><div className="t">MOSS 不合格时用 GPT 补图</div>
          <div className="d">每张图 MOSS 最多画三次，字还是不对时：开着就改用 GPT 按 MOSS 的画风画这一张（这一张要花钱，需要填 RunningHub 密钥）；关着就用错字最少的那张，并在检查报告里标红。</div></div>
        <div className="seg" role="radiogroup" aria-label="MOSS 不合格时用 GPT 补图">
          <button role="radio" disabled={!moss} aria-checked={prefs.mossFallbackGpt} aria-pressed={prefs.mossFallbackGpt} onClick={() => onPrefs({ mossFallbackGpt: true })}>开</button>
          <button role="radio" disabled={!moss} aria-checked={!prefs.mossFallbackGpt} aria-pressed={!prefs.mossFallbackGpt} onClick={() => onPrefs({ mossFallbackGpt: false })}>关</button>
        </div>
      </div>
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

function About({ info, prefs, onPrefs, onRecheckEnv, onOpenLogs, onToast, update: updateProp, updateActions: actionsProp, onCheckUpdate: checkProp }: SettingsProps): React.JSX.Element {
  // 没有从上层接到更新状态时（单独渲染这个页面），自己向 Main 要
  const [localUpdate, setLocalUpdate] = useState<UpdateState | null>(null)
  const update = updateProp === undefined ? localUpdate : updateProp
  const onCheckUpdate = checkProp ?? (() => window.a2v.checkUpdate(true).then(setLocalUpdate))
  const updateActions: UpdateActions = actionsProp ?? {
    onShowDetails: () => undefined,
    onDownload: () => { void window.a2v.downloadUpdate().then(setLocalUpdate, () => onToast('没能开始下载')) },
    onInstall: () => { void window.a2v.installUpdate().then(result => { if (!result.ok) onToast(result.reason === 'busy' ? '正在出片，出片结束后再安装' : '没能开始安装') }, () => onToast('没能开始安装')) },
    onLater: () => { void window.a2v.laterUpdate().then(setLocalUpdate, () => undefined) },
    onSkip: () => { void window.a2v.skipUpdate().then(setLocalUpdate, () => undefined) },
    onRetry: () => { void window.a2v.retryUpdate().then(setLocalUpdate, () => undefined) },
    onOpenLink: which => { void window.a2v.openUpdateLink(which).catch(() => onToast('没能打开浏览器')) }
  }
  const [result, setResult] = useState<string | null>(null)
  // R121M5：手动检查更新（和顶部提示条走同一个流程；状态在 Main）
  const [checkingUpdate, setCheckingUpdate] = useState(false)
  const [checkedOnce, setCheckedOnce] = useState(false)
  const checkUpdate = (): void => {
    setCheckingUpdate(true)
    setCheckedOnce(true)
    void onCheckUpdate().catch(() => onToast('没能检查更新')).finally(() => setCheckingUpdate(false))
  }
  // 手动点过"检查更新"，或者已经在下载 / 已下载 / 失败时，才在这一行下面显示结果
  const showUpdate = !!update && (checkedOnce || ['downloading', 'downloaded'].includes(update.phase) || (update.phase === 'failed' && update.failStage !== 'check'))
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
  // R120M P2：诊断报告（一个 .md 文件，默认存桌面）
  const exportReport = (): void => {
    setExporting(true)
    void window.a2v.exportDiagnosticsReport().then(outcome => {
      if (outcome.ok) setResult(`诊断报告已保存：${outcome.file}（不含密钥，可以直接发给作者）`)
      else if (!outcome.canceled) { setResult(null); onToast(outcome.error ?? '导出失败') }
    }, (reason: unknown) => onToast(reason instanceof Error ? reason.message : String(reason))).finally(() => setExporting(false))
  }
  const ready = info.engine.status === 'ready'
  return (
    <div className="box">
      <div className="box-row"><div className="grow"><div className="t">版本</div></div><span className="v">文章成片 {info.appVersion} · 引擎 {info.engine.version ?? '未知版本'}</span></div>
      <div className="box-row" style={{ flexWrap: 'wrap' }}>
        <div className="grow"><div className="t">检查更新</div><div className="d">去 GitHub 看有没有新版本。有的话可以在软件里直接更新，设置、密钥、工作文件夹和成片都会保留。</div></div>
        <button className="btn btn-sm" disabled={checkingUpdate || update?.phase === 'checking'} onClick={checkUpdate}>{checkingUpdate || update?.phase === 'checking' ? '正在检查…' : '检查更新'}</button>
        {showUpdate && !checkingUpdate && <div style={{ width: '100%' }} aria-live="polite"><UpdateAboutStatus state={update} actions={updateActions} /></div>}
      </div>
      <div className="box-row">
        <div className="grow"><div className="t">启动时自动检查更新</div><div className="d">打开软件约 10 秒后查一次；有新版本会在顶部提示，不会自己下载；连不上就算了，不会弹任何提示。关掉后仍可以在上面手动检查。</div></div>
        <div className="seg" role="radiogroup" aria-label="启动时自动检查更新">
          <button role="radio" aria-checked={prefs.autoCheckUpdate} aria-pressed={prefs.autoCheckUpdate} onClick={() => onPrefs({ autoCheckUpdate: true })}>开</button>
          <button role="radio" aria-checked={!prefs.autoCheckUpdate} aria-pressed={!prefs.autoCheckUpdate} onClick={() => onPrefs({ autoCheckUpdate: false })}>关</button>
        </div>
      </div>
      <div className="box-row">
        <div className="grow"><div className="t">运行环境</div><div className="d">{ready ? `引擎目录、Python 都找得到` : info.engine.problems.join('；')}</div></div>
        <span className={ready ? 'tone-ok' : 'tone-danger'}><span className={ready ? 'dot' : 'dot danger'} /> {ready ? '正常' : '有问题'}</span>
        <button className="btn btn-sm" onClick={onRecheckEnv}>重新检查</button>
      </div>
      <div className="box-row" style={{ flexWrap: 'wrap' }}>
        <div className="grow"><div className="t">遇到问题时</div><div className="d">导出诊断报告（一个 .md 文件，默认存到桌面），发给作者排查。不含密钥，用户文件夹路径也已隐去。</div></div>
        <button className="btn btn-sm btn-primary" disabled={exporting} onClick={exportReport}>{exporting ? '正在导出…' : '导出诊断报告'}</button>
        <button className="btn btn-sm" disabled={exporting} onClick={exportZip}>导出完整日志（zip）</button>
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
