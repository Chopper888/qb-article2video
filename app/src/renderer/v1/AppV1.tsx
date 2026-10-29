import { useCallback, useEffect, useRef, useState } from 'react'
import type { DemoRunScenario, KeyCheckOutcome } from '../../shared/v1/adapter'
import type { GuideAction } from '../../shared/v1/error-kinds'
import { configuredCount, KEY_FIELD_IDS, type KeyFieldId, type KeyStatusMap } from '../../shared/v1/keys'
import type { ChapterList } from '../../shared/v1/chapters'
import { missingKeysText, missingRequiredServices } from '../../shared/v1/required-keys'
import type { TaskRow, VideoItem } from '../../shared/v1/tasks'
import { runtimePrepBusy, type AppInfo, type DemoState, type ImportSourceKind, type RuntimePrep, type V1Prefs } from '../../shared/v1/types'
import type { ExternalLinkId } from '../../shared/v1/links'
import type { InputResolvedEvent } from '../../shared/v1/protocol'
import { DevPanel } from './DevPanel'
import { EngineStatus, Rail, RailThemeButton, RuntimePrepNotice, ThemeButton, Toast, TopBar, type Page } from './parts'
import { Home } from './pages/Home'
import { ConfirmView, Loading, ProblemsView } from './pages/ImportCheck'
import { LibraryPage, TasksPage } from './pages/Lists'
import { Onboarding } from './pages/Onboarding'
import { SettingsPage, type SettingsTab } from './pages/Settings'
import { Workbench } from './pages/Workbench'
import { applyTheme, DARK, LIGHT, LIGHT_THEME_AVAILABLE } from './theme'
import { useFlow } from './use-flow'

type View = { kind: 'onboarding'; step: number } | { kind: 'page'; page: Page } | { kind: 'flow' }

export interface DevOverrides { keyStatus?: KeyStatusMap; reportExpanded?: boolean; runtimePrep?: RuntimePrep }

export function AppV1(): React.JSX.Element {
  const [info, setInfo] = useState<AppInfo | null>(null)
  const [prefs, setPrefs] = useState<V1Prefs | null>(null)
  const [keyStatus, setKeyStatus] = useState<KeyStatusMap | null>(null)
  const [check, setCheck] = useState<KeyCheckOutcome | null>(null)
  const [checking, setChecking] = useState(false)
  const [view, setView] = useState<View>({ kind: 'page', page: 'home' })
  const [tab, setTab] = useState<SettingsTab>('keys')
  const [toast, setToast] = useState<string | null>(null)
  const [fatal, setFatal] = useState<string | null>(null)
  const [devOpen, setDevOpen] = useState(false)
  const [dev, setDev] = useState<DevOverrides>({})
  const [demoState, setDemoState] = useState<DemoState | null>(null)
  const [chapters, setChapters] = useState<ChapterList | null>(null)
  const [playing, setPlaying] = useState<VideoItem | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const remakeRef = useRef<(row: TaskRow) => void>(() => undefined)
  // APP3：安装版首次启动准备运行环境的状态（Main 推进度）
  const [runtimePrep, setRuntimePrep] = useState<RuntimePrep | null>(null)
  const shownPrep = dev.runtimePrep ?? runtimePrep
  const runtimeBusy = runtimePrepBusy(shownPrep)

  const say = useCallback((text: string) => {
    setToast(text)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3200)
  }, [])
  const flow = useFlow(message => say(`操作没完成：${message}`), !runtimeBusy)

  useEffect(() => {
    const off = window.a2v.onRuntimePrep(setRuntimePrep)
    void window.a2v.getRuntimePrep().then(state => setRuntimePrep(current => current ?? state), () => undefined)
    return off
  }, [])
  const retryRuntime = useCallback(() => {
    setDev(current => ({ ...current, runtimePrep: undefined }))
    void window.a2v.retryRuntimePrep().then(setRuntimePrep, (reason: unknown) => say(reason instanceof Error ? reason.message : String(reason)))
  }, [say])

  // APP4：设置不管在哪里改，Main 都会推过来，确认页、成片库、任务列表立刻用新值
  useEffect(() => window.a2v.onPrefsChanged(setPrefs), [])

  useEffect(() => {
    void Promise.all([window.a2v.getAppInfo(), window.a2v.getPrefs(), window.a2v.getKeyStatus()]).then(([appInfo, loaded, keys]) => {
      setInfo(appInfo); setPrefs(loaded); setKeyStatus(keys)
      if (!loaded.onboarded) setView({ kind: 'onboarding', step: 0 })
    }).catch((reason: unknown) => setFatal(reason instanceof Error ? reason.message : String(reason)))
  }, [])

  const theme = prefs && LIGHT_THEME_AVAILABLE ? prefs.theme : 'dark'
  useEffect(() => { applyTheme(theme === 'light' ? LIGHT : DARK); document.documentElement.dataset.theme = theme }, [theme])

  useEffect(() => { if (info?.devTools) void window.a2v.getDemoState().then(setDemoState) }, [info?.devTools, devOpen])
  const confirmArticle = flow.flow.kind === 'confirm' ? flow.flow.resolved.article : null
  useEffect(() => {
    setChapters(null)
    if (confirmArticle) void window.a2v.readChapters(confirmArticle).then(setChapters, () => setChapters(null))
  }, [confirmArticle])

  useEffect(() => {
    if (!info?.devTools) return
    const onKey = (event: KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'd') { event.preventDefault(); setDevOpen(open => !open) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [info?.devTools])

  const updatePrefs = useCallback(async (patch: Partial<V1Prefs>) => {
    try { setPrefs(await window.a2v.updatePrefs(patch)) } catch (reason) { say(reason instanceof Error ? reason.message : String(reason)) }
  }, [say])
  const saveKey = useCallback(async (id: KeyFieldId, value: string) => {
    try {
      setKeyStatus(await window.a2v.setKey(id, value))
      setDev(current => ({ ...current, keyStatus: undefined }))
      // 改过的那项，旧的检测结果不再作数
      const service = id.startsWith('feishu') ? 'feishu' : id
      setCheck(current => current ? { ...current, results: current.results.filter(item => item.service !== service) } : current)
    } catch (reason) { say(reason instanceof Error ? reason.message : String(reason)) }
  }, [say])
  const runCheck = useCallback(async () => {
    setChecking(true)
    try { setCheck(await window.a2v.checkKeys()) } catch (reason) { say(reason instanceof Error ? reason.message : String(reason)) } finally { setChecking(false) }
  }, [say])
  const signup = useCallback(() => { void window.a2v.openExternal('runninghubSignup').catch(() => say('没能打开浏览器')) }, [say])
  const openLink = useCallback((link: ExternalLinkId) => { void window.a2v.openExternal(link).catch(() => say('没能打开浏览器')) }, [say])
  const chooseFolder = useCallback(async (target: 'work' | 'output') => {
    try { setPrefs(await window.a2v.chooseFolder(target)) } catch (reason) { say(reason instanceof Error ? reason.message : String(reason)) }
  }, [say])
  const openFolder = useCallback((target: 'work' | 'output' | 'logs') => { void window.a2v.openFolder(target).catch((reason: unknown) => say(reason instanceof Error ? reason.message : String(reason))) }, [say])

  const navigate = useCallback((page: Page) => { setView({ kind: 'page', page }); if (page === 'home') flow.reset() }, [flow])
  const openKeys = useCallback(() => { setTab('keys'); setView({ kind: 'page', page: 'settings' }) }, [])

  // 开发者开关：截图和测试用的场景
  const applyScene = useCallback(async (scene: string) => {
    const full = KEY_FIELD_IDS.reduce((acc, id) => ({ ...acc, [id]: { filled: true, last4: id.startsWith('feishu') ? null : 'a1b2' } }), {} as KeyStatusMap)
    const oneMissing = { ...full, feishu_app_secret: { filled: false, last4: null } }
    const oneMissingRequired = { ...full, tts: { filled: false, last4: null }, runninghub: { filled: false, last4: null } }
    setDev(current => ({ runtimePrep: current.runtimePrep }))
    setCheck(current => current)
    const go = async (run: DemoRunScenario): Promise<void> => {
      await window.a2v.setDemoState({ runScenario: run, delayMs: 0 })
      const shot = run === 'run_theory'
      await flow.startRun(shot ? '示例-演示文章B' : '示例-演示文章A', shot ? '示例演示文章：几种常用工具的搭配' : '示例演示文章：如何整理电脑桌面', prefs?.maxCostCny ?? 50,
        { schema_version: 1, seq: 1, time: '', event: 'estimate', step: null, message: '', low_cny: 15, high_cny: 30, by_service: {}, saved_by_cache_cny: 0, basis: {} })
      setView({ kind: 'flow' })
    }
    const [name, arg] = scene.split(':')
    switch (name) {
      case 'onboard': setView({ kind: 'onboarding', step: Number(arg ?? 0) }); if (arg === '1-check') { setView({ kind: 'onboarding', step: 1 }); await runCheck() } break
      case 'home-full': flow.reset(); setDev({ keyStatus: full }); setView({ kind: 'page', page: 'home' }); break
      case 'home-missing': flow.reset(); setDev({ keyStatus: oneMissing }); setView({ kind: 'page', page: 'home' }); break
      case 'home': flow.reset(); setView({ kind: 'page', page: 'home' }); break
      case 'import': await window.a2v.setDemoState({ importScenario: arg as 'import_pass', delayMs: 0, mode: 'demo' }); setView({ kind: 'flow' }); await flow.startImport('https://xxx.feishu.cn/wiki/演示文章'); break
      case 'import-full': await window.a2v.setDemoState({ importScenario: 'import_pass', delayMs: 0, mode: 'demo' }); setView({ kind: 'flow' }); await flow.startImport('https://xxx.feishu.cn/wiki/演示文章'); setDev({ keyStatus: full }); break
      case 'import-missing': setDev({ keyStatus: oneMissingRequired }); await window.a2v.setDemoState({ importScenario: 'import_pass', delayMs: 0, mode: 'demo' }); setView({ kind: 'flow' }); await flow.startImport('https://xxx.feishu.cn/wiki/演示文章'); break
      case 'run': await window.a2v.setDemoState({ mode: 'demo' }); await go(arg as DemoRunScenario); if (arg === 'done_red') setDev({ reportExpanded: true }); break
      // 真实流程（开发截图用）：real-import:<文件夹或链接>、confirm-run、stop、resume、mode:real|demo、nopaid:on|off
      case 'mode': setDemoState(await window.a2v.setDemoState({ mode: arg === 'demo' ? 'demo' : 'real' })); break
      case 'nopaid': setDemoState(await window.a2v.setDemoState({ noPaid: arg === 'on' })); break
      // APP4：开发截图——拖进多个文件 drop:<路径1>|<路径2>…；导入本地文章的选择框 choose:any|folder|file
      case 'drop': {
        const paths = scene.slice('drop:'.length).split('|').filter(Boolean)
        setDemoState(await window.a2v.setDemoState({ mode: 'real' })); setView({ kind: 'flow' })
        if (paths.length === 1) await flow.startImport(paths[0]!); else await flow.startImportDropped(paths)
        break
      }
      case 'choose': void window.a2v.chooseImportSource((arg as ImportSourceKind) ?? 'any'); break
      // APP4：复制粘贴实测用——把焦点放到启动页的链接输入框
      case 'focus-input': document.querySelector<HTMLInputElement>('.big-input input')?.focus(); break
      case 'remake': {
        const row = (await window.a2v.listTasks()).find(task => task.article === arg)
        if (row) remakeRef.current(row)
        break
      }
      case 'real-import': setDemoState(await window.a2v.setDemoState({ mode: 'real' })); setView({ kind: 'flow' }); await flow.startImport(scene.slice('real-import:'.length)); break
      case 'real-estimate': {
        setDemoState(await window.a2v.setDemoState({ mode: 'real' }))
        const row = (await window.a2v.listTasks()).find(task => task.article === arg)
        if (!row) { say(`工作文件夹里没有 ${arg}`); break }
        setView({ kind: 'flow' })
        await flow.estimateArticle({ schema_version: 1, seq: 0, time: '', event: 'input_resolved', step: null, message: '', article: row.article,
          work_dir: `${prefs?.workDir ?? ''}/${row.article}`, source_type: row.source === '飞书' ? 'feishu' : 'markdown', title: row.title ?? row.article,
          reused: true, changed: false, image_count: 0, content_type: row.contentType, warnings: [] })
        break
      }
      case 'confirm-run': await flow.confirmRun(prefs?.maxCostCny ?? 50); setView({ kind: 'flow' }); break
      case 'stop': await flow.stop(); break
      case 'resume': await flow.resume(); setView({ kind: 'flow' }); break
      case 'flow': setView({ kind: 'flow' }); break
      case 'export-diag': setTab('about'); setView({ kind: 'page', page: 'settings' }); setTimeout(() => window.dispatchEvent(new Event('a2v-dev-export')), 300); break
      case 'tasks': setView({ kind: 'page', page: 'tasks' }); break
      case 'library': setView({ kind: 'page', page: 'library' }); break
      case 'settings': setTab((arg as SettingsTab) ?? 'keys'); setView({ kind: 'page', page: 'settings' }); if (arg === 'keys-check') { setTab('keys'); await runCheck() } break
      case 'theme': await updatePrefs({ theme: arg === 'light' ? 'light' : 'dark' }); break
      case 'devpanel': setDevOpen(arg === 'on'); break
      // APP3：运行环境准备的显示（只改显示，不动 Main）：runtime:copying:<百分比>、runtime:failed、runtime:off
      case 'runtime': {
        const [, kind, value] = scene.split(':')
        setDev(current => ({ ...current, runtimePrep: kind === 'copying' ? { status: 'copying', percent: Number(value ?? 40), doneFiles: Math.round(19124 * Number(value ?? 40) / 100), totalFiles: 19124 }
          : kind === 'failed' ? { status: 'failed', error: value ? scene.slice('runtime:failed:'.length) : '复制运行环境失败：ENOSPC: no space left on device, copyfile' } : undefined }))
        break
      }
      // APP2b：点页面上文字等于 arg 的按钮（截"查看详情"展开用）
      case 'click': Array.from(document.querySelectorAll('button')).find(button => button.textContent?.trim() === arg)?.click(); break
    }
  }, [flow, prefs?.maxCostCny, runCheck, updatePrefs])

  useEffect(() => window.a2v.onDevScene(scene => { void applyScene(scene) }), [applyScene])

  if (fatal) return <div className="center-fill">文章成片启动失败：{fatal}</div>
  if (!info || !prefs || !keyStatus) return <div className="center-fill">正在加载…</div>

  const shownKeys = dev.keyStatus ?? keyStatus
  const { configured, total } = configuredCount(shownKeys)
  const keysProps = { status: shownKeys, check, checking, onSave: saveKey, onCheck: () => void runCheck(), onSignup: signup, onOpenLink: openLink }
  const toggleTheme = (): void => { void updatePrefs({ theme: theme === 'dark' ? 'light' : 'dark' }) }
  const engine = <EngineStatus version={info.engine.version} ready={info.engine.status === 'ready'} />
  const themeButton = LIGHT_THEME_AVAILABLE ? <ThemeButton theme={theme} onToggle={toggleTheme} /> : null
  const rhCheck = check?.results.find(item => item.service === 'runninghub')
  const diagnostics = [
    `文章成片 ${info.appVersion}（${info.isPackaged ? '安装版' : '开发模式'}，${info.platform}）`,
    `引擎 ${info.engine.version ?? '未知'}：${info.engine.status === 'ready' ? '正常' : info.engine.problems.join('；')}`,
    `密钥：${configured}/${total} 已配置（只记是否填写，不含内容）`,
    `上次检测：${prefs.lastKeyCheck ? Object.entries(prefs.lastKeyCheck.statuses).map(([k, v]) => `${k}=${v}`).join(' ') : '没有'}`,
    `导入 / 估算 / 出片：${info.adapterKinds.article === 'demo' ? '演示场景' : '真实引擎'}`
  ].join('\n')

  const prepNotice = <RuntimePrepNotice prep={shownPrep} onRetry={retryRuntime} />
  const handleGuide = (action: GuideAction, value?: number): void => {
    const current = flow.flow
    // APP3：运行环境准备好之前不能"继续"（按钮已经不能点，这里再拦一次）
    if (runtimeBusy && (action === 'resume' || action === 'raise_limit')) { say('运行环境还在准备，好了再继续'); return }
    switch (action) {
      case 'resume': void flow.resume(); break
      // 到费用上限：改上限和继续合并成一个操作
      case 'raise_limit': if (current.kind === 'run' && value && value > 0) { flow.setMaxCost(value); void flow.resume(value) } break
      case 'open_topup': signup(); break
      case 'open_keys': openKeys(); break
      case 'open_cleanup': setTab('folders'); setView({ kind: 'page', page: 'settings' }); break
      case 'reimport': if (current.kind === 'problems') void flow.startImport(current.input); else if (current.kind === 'run') navigate('home'); break
      case 'copy_diagnostics': void navigator.clipboard?.writeText(diagnostics).then(() => say('诊断信息已复制（不含密钥）'), () => say('没能复制')); break
      case 'open_logs': openFolder('logs'); break
      case 'dismiss': navigate('home'); break
    }
  }

  const startImport = (input: string): void => { setView({ kind: 'flow' }); void flow.startImport(input) }
  const startDropped = (paths: string[]): void => { setView({ kind: 'flow' }); void flow.startImportDropped(paths) }
  const chooseSource = (kind: ImportSourceKind): void => {
    void window.a2v.chooseImportSource(kind).then(path => { if (path) startImport(path) }, () => say('没能打开选择框'))
  }
  // APP4："重新出片"：已完成（含清理过中间文件）的文章 → 先估算费用 → 确认 → 跑（和续跑同一条命令，正常从缓存重建）
  const remake = (row: { article: string; title: string | null; contentType: 'theory' | 'screenshot' | null; source: '飞书' | 'Markdown' | null }): void => {
    if (runtimeBusy) { say('运行环境还在准备，好了再重新出片'); return }
    const resolved: InputResolvedEvent = { schema_version: 1, seq: 0, time: '', event: 'input_resolved', step: null, message: '', article: row.article,
      work_dir: `${prefs.workDir}/${row.article}`, source_type: row.source === '飞书' ? 'feishu' : 'markdown', title: row.title ?? row.article,
      reused: true, changed: false, image_count: 0, content_type: row.contentType, warnings: [] }
    setView({ kind: 'flow' })
    void flow.estimateArticle(resolved)
  }
  remakeRef.current = remake
  const remakeCurrent = (): void => {
    const current = flow.flow
    if (current.kind !== 'run') return
    void window.a2v.articleInfo(current.article).then(info => remake({ article: current.article, title: info?.title ?? current.title, contentType: info?.contentType ?? current.view.contentType, source: info?.source ?? null }),
      () => remake({ article: current.article, title: current.title, contentType: current.view.contentType, source: null }))
  }
  const openTask = (row: TaskRow): void => {
    const current = flow.flow
    if (row.state === 'running' && current.kind === 'run' && current.article === row.article) { setView({ kind: 'flow' }); return }
    if (row.state === 'stopped' || row.state === 'failed') {
      if (runtimeBusy) { say('运行环境还在准备，好了再继续'); return }
      setView({ kind: 'flow' })
      void flow.resumeArticle(row.article, row.title ?? row.article, prefs.maxCostCny)
      return
    }
    navigate('library')
  }

  let body: React.JSX.Element
  if (view.kind === 'onboarding') {
    body = <Onboarding step={view.step} onStep={step => setView({ kind: 'onboarding', step })} keys={keysProps} prefs={prefs} onChooseFolder={target => void chooseFolder(target)}
      onFinish={() => { void updatePrefs({ onboarded: true }); setView({ kind: 'page', page: 'home' }) }} />
  } else if (view.kind === 'flow' && flow.flow.kind !== 'idle') {
    const current = flow.flow
    let inner: React.JSX.Element
    if (current.kind === 'importing') inner = <Loading text="正在读取文章、检查图片…" />
    else if (current.kind === 'estimating') inner = current.waiting
      ? <div className="work-body" style={{ flexDirection: 'column', gap: 16 }}>{prepNotice}<Loading text="检查通过，运行环境准备好后自动估算费用…" /></div>
      : <Loading text="检查通过，正在估算费用…" />
    else if (current.kind === 'problems') {
      inner = <ProblemsView resolved={current.view.resolved} problems={current.view.problems ?? []} engineMessage={current.view.runDone?.message ?? null}
        onOpenFolder={() => void window.a2v.openArticleSource(current.input).catch((reason: unknown) => say(reason instanceof Error ? reason.message : String(reason)))}
        onRecheck={() => void flow.startImport(current.input)} />
    }
    else if (current.kind === 'confirm') {
      // 缺必需的密钥时不能开始（开发者开关"不付费试跑"时引擎不调付费服务，不拦）
      const missing = demoState?.noPaid && info.devTools ? [] : missingRequiredServices({ sourceType: current.resolved.source_type, imageCount: current.resolved.image_count }, shownKeys)
      inner = <ConfirmView resolved={current.resolved} estimate={current.estimate} defaultMaxCost={prefs.maxCostCny} outputDir={prefs.outputDir}
        missingKeysText={missing.length ? missingKeysText(missing) : null} onOpenKeys={openKeys} chapters={chapters}
        keysLine={{ text: `${configured}/${total} 已配置`, ok: configured === total }}
        runninghubLine={rhCheck ? { text: rhCheck.status === 'ok' ? '密钥可用' : rhCheck.status === 'missing' ? '未填写' : '有问题，去检测', tone: rhCheck.status === 'ok' ? 'tone-ok' : 'tone-warn' } : { text: shownKeys.runninghub.filled ? '已填写' : '未填写', tone: shownKeys.runninghub.filled ? 'muted' : 'tone-warn' }}
        onBack={() => navigate('home')} onConfirm={max => { if (!runtimeBusy) void flow.confirmRun(max) }} runtimeBusy={runtimeBusy} runtimeNotice={prepNotice} />
    } else {
      inner = <Workbench key={current.startedAt} title={current.title} view={current.view} maxCostCny={current.maxCostCny} estimate={current.estimate} ended={current.ended} stopping={current.stopping} reportExpanded={dev.reportExpanded}
        resumeBlocked={runtimeBusy} runtimeNotice={prepNotice}
        onAction={handleGuide} onStop={() => void flow.stop()} onOpenOutput={() => openFolder('output')} onNext={() => navigate('home')} onRemake={remakeCurrent} />
    }
    body = (
      <div className="workspace">
        <Rail onNavigate={page => { if (page === 'settings') setTab('keys'); navigate(page) }} theme={LIGHT_THEME_AVAILABLE ? <RailThemeButton theme={theme} onToggle={toggleTheme} /> : undefined} />
        <div className="work-main">{inner}</div>
      </div>
    )
  } else {
    const page = view.kind === 'page' ? view.page : 'home'
    body = (
      <>
        <TopBar page={page} onNavigate={navigate} engine={engine} right={themeButton} />
        {page === 'home' && <Home runtimeBusy={runtimeBusy} runtimeNotice={prepNotice} keysConfigured={configured} keysTotal={total} busy={false} onOpenKeys={openKeys} onAllTasks={() => navigate('tasks')}
          onStart={startImport} platform={info.platform} onChooseSource={chooseSource} onDropFiles={startDropped} />}
        {page === 'tasks' && <TasksPage onOpen={openTask} onRemake={remake} resumeBlocked={runtimeBusy} workDir={prefs.workDir} platform={info.platform} onToast={say} />}
        {page === 'library' && <LibraryPage outputDir={prefs.outputDir} onOpenFolder={() => openFolder('output')} onPlay={setPlaying} />}
        {page === 'settings' && <SettingsPage tab={tab} onTab={setTab} keys={keysProps} prefs={prefs} info={info} lightAvailable={LIGHT_THEME_AVAILABLE}
          onPrefs={patch => void updatePrefs(patch)} onChooseFolder={target => void chooseFolder(target)} onOpenLogs={() => openFolder('logs')}
          onRecheckEnv={() => void window.a2v.getAppInfo().then(setInfo)} onToast={say} diagnostics={diagnostics} />}
      </>
    )
  }

  // APP4：开发者"不付费试跑"打开时顶部常驻提示（只在开发者模式出现）
  const noPaidOn = info.devTools && !!demoState?.noPaid
  return (
    <div className={noPaidOn ? 'v1 has-dev-banner' : 'v1'}>
      {noPaidOn && <div className="dev-banner" role="status">开发者：不付费试跑已打开</div>}
      {body}
      <Toast text={toast} />
      {playing && (
        <div className="overlay" onClick={() => setPlaying(null)}>
          <div className="dialog" role="dialog" aria-label={playing.title} onClick={event => event.stopPropagation()} style={{ maxWidth: 'none' }}>
            <h2>{playing.title}</h2>
            <video className="player" src={window.a2v.mediaUrl(playing.file)} controls autoPlay />
            <button className="btn" style={{ alignSelf: 'flex-end' }} onClick={() => setPlaying(null)}>关闭</button>
          </div>
        </div>
      )}
      {info.devTools && devOpen && <DevPanel onScene={scene => void applyScene(scene)} onClose={() => setDevOpen(false)} onState={setDemoState} />}
    </div>
  )
}
