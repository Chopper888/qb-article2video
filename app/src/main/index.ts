import { app, BrowserWindow, dialog, ipcMain, Menu, net, Notification, powerSaveBlocker, protocol, safeStorage, shell } from 'electron'
import type { IpcMainInvokeEvent } from 'electron'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { IPC } from '../shared/ipc'
import type { AppSettings, CreateTaskInput, Diagnostics } from '../shared/types'
import { JsonStorage } from '../storage/json-storage'
import { TaskManager } from './task-manager'
import { createApplicationManager } from './real-sample-service'
import { QUIT_BUTTONS, QuitCoordinator } from './quit-coordinator'
import { initializeAppPaths } from './app-paths'
import { resolveDevelopmentResources } from './development-resources'
import { resolvePackagedResources, type PackagedResources } from './packaged-resources'
import { EnvironmentReadinessService } from './environment-readiness'
import { startTaskWithReadiness } from './start-task'
import { RuntimeWorkspace } from './runtime-workspace'
import { createAppLog } from './v1/app-log'
import { appMenuTemplate } from './v1/app-menu'
import { installEditShortcuts } from './v1/edit-shortcuts'
import { DevNoPaid } from './v1/dev-no-paid'
import { DemoEngineAdapter } from './v1/demo-engine-adapter'
import { runDevCapture } from './v1/dev-capture'
import { importKeysFromEnvFile } from './v1/dev-key-import'
import { describeEngine, readEngineVersion, resolveDevEngineLocation, type EngineLocation } from './v1/engine-location'
import { EngineKeyCheck } from './v1/key-check-runner'
import { defaultPrefs, PrefsStore } from './v1/prefs-store'
import { registerV1Ipc } from './v1/register-v1'
import { SecretStore } from './v1/secret-store'
import { RealEngineAdapter, type FinishedRun } from './v1/real-engine-adapter'
import { RoutedEngineAdapter } from './v1/routed-adapter'
import { cleanupPreview, deleteTask, deleteTaskCheck, diskUsage, listVideos, readArticleTitle, readReportSummary, readTaskFacts, runCleanup, scanTasks, videoArticle } from './v1/work-scan'
import { coverDataUrl, probeDuration } from './v1/media'
import { collectDiagnostics, writeDiagnosticsZip } from './v1/diagnostics-zip'
import { RuntimePreparer } from './v1/runtime-copy'
import { createLicenseCatalog, packagedLicenseRoots, versionsFromAssets, type LicenseRoots } from './v1/licenses'
import { resolveRuntimeDirectory } from './runtime-workspace'
import { NodeProcessTransport } from '../engine/process-transport'
import { TaskkillProcessTreeTerminator } from '../engine/process-tree-terminator'
import { errorGuide } from '../shared/v1/error-kinds'
import { IPC_V1, MEDIA_SCHEME } from '../shared/v1/ipc'
import type { VideoItem } from '../shared/v1/tasks'
import type { AppInfo } from '../shared/v1/types'

let mainWindow: BrowserWindow | null = null
let manager: TaskManager | null = null
let quitCoordinator: QuitCoordinator | null = null
// 1.0 界面的真实引擎（setupV1 里建）：关闭保护要知道它是否在出片
let v1Real: RealEngineAdapter | null = null
let v1MediaRoots: () => string[] = () => []

// 成片播放：a2v-media://local/?p=<路径>（必须在 ready 之前登记）
// 旧测试的 electron 替身没有 protocol：登记失败只影响成片播放，不影响旧主进程能力
try { protocol.registerSchemesAsPrivileged([{ scheme: MEDIA_SCHEME, privileges: { stream: true, supportFetchAPI: true, standard: true, secure: true } }]) } catch { /* 没有 protocol */ }

const paths = initializeAppPaths(app)
const resources = paths.isPackaged ? resolvePackagedResources(paths) : resolveDevelopmentResources(paths)
const runtimeWorkspace = new RuntimeWorkspace(resources)
const readiness = new EnvironmentReadinessService(paths, resources, process.env, undefined, runtimeWorkspace)
// 1.0 界面：开发模式下引擎位置可配置；安装版沿用已解析的打包资源
// 安装版的 runtimeDir 在复制完成后才填（见 setupV1 里的 ensureRuntimeCopy）
const engineLocation: EngineLocation = paths.isPackaged
  ? { root: resources?.engineRoot ?? null, python: resources?.pythonExecutable ?? null,
      runtimeDir: null, ffmpegDir: resources && 'ffmpegBin' in resources ? (resources as { ffmpegBin: string }).ffmpegBin : null }
  : resolveDevEngineLocation(paths.resourcesRoot)
const devTools = !paths.isPackaged || process.env.A2V_DEV_TOOLS === '1'
const appLog = createAppLog(join(paths.userDataRoot, 'logs'))

function getManager(): TaskManager {
  if (!manager) throw new Error('APP 状态尚未初始化')
  return manager
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1280,
    minHeight: 800,
    useContentSize: true,
    // 开发截图要 1440×900 整窗，允许超出屏幕可用高度
    enableLargerThanScreen: !paths.isPackaged && !!process.env.A2V_DEV_CAPTURE,
    show: false,
    backgroundColor: '#1C1C1E',
    title: '文章成片',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  })

  window.once('ready-to-show', () => window.show())
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event) => event.preventDefault())
  installEditShortcuts(window.webContents)
  window.on('close', (event) => guardQuit(event))

  if (process.env.ELECTRON_RENDERER_URL) {
    void window.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    void window.loadFile(join(__dirname, '../renderer/index.html'))
  }
  return window
}

function registerIpc(): void {
  const trusted = (event: IpcMainInvokeEvent): TaskManager => {
    if (!mainWindow || event.sender !== mainWindow.webContents || event.senderFrame !== mainWindow.webContents.mainFrame) {
      throw new Error('拒绝未知窗口的 IPC 请求')
    }
    return getManager()
  }
  ipcMain.handle(IPC.getState, (event) => trusted(event).snapshot())
  ipcMain.handle(IPC.createTask, (event, input: CreateTaskInput) => trusted(event).createTask(input))
  ipcMain.handle(IPC.createSampleTask, (event, input: unknown) => trusted(event).createSampleTask(input))
  ipcMain.handle(IPC.getEnvironmentReadiness, (event) => { trusted(event); return readiness.check() })
  ipcMain.handle(IPC.startTask, (event, taskId: string) => startTaskWithReadiness(trusted(event), taskId, readiness))
  ipcMain.handle(IPC.stopTask, (event, taskId: string) => trusted(event).stopTask(taskId))
  ipcMain.handle(IPC.retryTask, (event, taskId: string) => trusted(event).retryTask(taskId))
  ipcMain.handle(IPC.updateSettings, (event, patch: Partial<AppSettings>) => trusted(event).updateSettings(patch))
  ipcMain.handle(IPC.getDiagnostics, (event): Diagnostics => {
    trusted(event)
    const state = getManager().snapshot()
    const running = state.tasks.find((task) => task.status === 'running')
    return {
      appVersion: app.getVersion(),
      mode: 'Mock + Real',
      electronVersion: process.versions.electron,
      nodeVersion: process.versions.node,
      userDataPath: paths.userDataRoot,
      theme: state.settings.theme,
      taskCount: state.tasks.length,
      runningTask: running?.title ?? null,
      lastError: state.lastError
    }
  })
}

if (!app.requestSingleInstanceLock()) app.quit()
else void app.whenReady().then(async () => {
  const storage = new JsonStorage(join(paths.userDataRoot, 'app-state-v1.json'))
  manager = await createApplicationManager(storage, { realTaskRoot: paths.realTaskRoot, resources, runtimeWorkspace })
  // 关闭保护：旧任务优先；没有旧任务时看 1.0 界面是否在出片（复用同一个"停止任务并退出"对话框）
  const legacy = manager
  const quitManager = {
    getActiveRealTaskId: () => legacy.getActiveRealTaskId() ?? (v1Real?.activeRun() ? `v1:${v1Real.activeRun()!.sessionId}` : null),
    stopTask: (taskId: string) => taskId.startsWith('v1:') ? v1Real!.stop(taskId.slice(3)) : legacy.stopTask(taskId),
    close: () => legacy.close()
  }
  quitCoordinator = new QuitCoordinator(quitManager, {
    prompt: async () => {
      const options = {
        type: 'warning' as const,
        title: '真实任务仍在运行',
        message: '关闭 APP 前必须先停止正在运行的真实任务。',
        detail: '停止会结束当前任务的整棵进程树，并保留已经产生的任务文件和运行事实。',
        buttons: [...QUIT_BUTTONS],
        defaultId: 1,
        cancelId: 1,
        noLink: true
      }
      const result = mainWindow && !mainWindow.isDestroyed()
        ? await dialog.showMessageBox(mainWindow, options)
        : await dialog.showMessageBox(options)
      return result.response === 0 ? 'stop-and-quit' : 'cancel'
    },
    showError: (title, message) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.show()
        mainWindow.focus()
        void dialog.showMessageBox(mainWindow, { type: 'error', title, message, buttons: ['知道了'] })
      } else dialog.showErrorBox(title, message)
    },
    quit: () => app.quit()
  })
  registerIpc()
  // APP4：不要 File / Edit / View / Window 菜单（Mac 留应用菜单 + 编辑，否则输入框里复制粘贴会失效）
  try {
    const template = appMenuTemplate(process.platform)
    Menu.setApplicationMenu(template ? Menu.buildFromTemplate(template) : null)
  } catch (error) { appLog.write(`菜单设置失败：${error instanceof Error ? error.message : String(error)}`) }
  try { registerMediaProtocol() } catch (error) { appLog.write(`成片播放协议登记失败：${error instanceof Error ? error.message : String(error)}`) }
  // 1.0 界面的初始化出错时只记日志，不影响旧主进程能力（任务、停止、关闭保护、恢复）
  try { setupV1() } catch (error) { appLog.write(`1.0 界面初始化失败：${error instanceof Error ? error.message : String(error)}`) }
  manager.subscribe((state) => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send(IPC.stateChanged, state)
    }
  })
  mainWindow = createWindow()
  const capturePlan = process.env.A2V_DEV_CAPTURE
  if (!paths.isPackaged && capturePlan) {
    const window = mainWindow
    window.webContents.once('did-finish-load', () => {
      void runDevCapture(window, capturePlan, appLog.write).catch((error: unknown) => appLog.write(`dev-capture 失败：${error instanceof Error ? error.message : String(error)}`))
        .finally(() => app.exit(0))
    })
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) mainWindow = createWindow()
  })
}).catch((error: unknown) => {
  dialog.showErrorBox('文章成片启动失败', error instanceof Error ? error.message : String(error))
  app.quit()
})

app.on('before-quit', (event) => guardQuit(event))

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

function setupV1(): void {
  const secrets = new SecretStore(join(paths.userDataRoot, 'secrets-v1.json'), safeStorage)
  const devKeyFile = process.env.A2V_DEV_IMPORT_KEYS
  if (!paths.isPackaged && devKeyFile) {
    try { appLog.write(`dev-key-import：导入了 ${importKeysFromEnvFile(devKeyFile, secrets).join('、') || '0 项'}`) } catch { appLog.write('dev-key-import：读取失败') }
  }
  const prefs = new PrefsStore(join(paths.userDataRoot, 'prefs-v1.json'), defaultPrefs())
  const demo = new DemoEngineAdapter()
  // APP4："不付费试跑"只在内存里，每次启动都是关的；非开发者模式时永远不生效
  const noPaid = new DevNoPaid(devTools)
  const engineVersion = readEngineVersion(engineLocation.root)
  // APP3：首次启动在后台复制运行环境，界面显示进度；准备好之前"预估费用""确认并开始出片""继续"都不能点。
  // 准备好之后删除 wzcp 下其他版本的 rt-* 目录。开发模式不需要（截图、实测可用 A2V_DEV_RUNTIME_SOURCE/TARGET 模拟）。
  let preparer: RuntimePreparer | null = null
  const devPrep = !paths.isPackaged && process.env.A2V_DEV_RUNTIME_SOURCE && process.env.A2V_DEV_RUNTIME_TARGET
    ? { source: process.env.A2V_DEV_RUNTIME_SOURCE, target: process.env.A2V_DEV_RUNTIME_TARGET } : null
  try {
    const plan = paths.isPackaged && resources ? { source: resources.runtimeSource, target: resolveRuntimeDirectory() } : devPrep
    if (plan) {
      preparer = new RuntimePreparer({
        ...plan, log: appLog.write,
        // 开发模拟只演示复制和清理，不改开发用的运行环境
        onReady: dir => { if (paths.isPackaged) engineLocation.runtimeDir = dir },
        inUse: dir => { const active = engineLocation.runtimeDir; return !!active && (active === dir || active.startsWith(dir + (process.platform === 'win32' ? '\\' : '/'))) }
      })
      preparer.subscribe(state => { if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(IPC_V1.runtimePrep, state) })
      // 开发模拟时等窗口好了再开始，截图能看到进度
      const delay = devPrep ? Number(process.env.A2V_DEV_RUNTIME_DELAY_MS ?? 0) : 0
      setTimeout(() => { void preparer!.start() }, Number.isFinite(delay) ? delay : 0)
    }
  } catch (error) { appLog.write(`运行环境位置无效：${error instanceof Error ? error.message : String(error)}`) }
  const logsDir = join(paths.userDataRoot, 'logs')
  let lastRun: FinishedRun | null = null
  const real = new RealEngineAdapter({
    location: () => engineLocation,
    secrets: () => secrets.engineEnv(),
    deliveryRoot: () => prefs.get().outputDir,
    noPaid: () => noPaid.enabled,
    // APP4：出片期间阻止电脑睡眠（显示器照常可以关）
    keepAwake: {
      start: () => powerSaveBlocker.start('prevent-app-suspension'),
      stop: id => { if (powerSaveBlocker.isStarted(id)) powerSaveBlocker.stop(id) }
    },
    transport: new NodeProcessTransport(),
    terminator: new TaskkillProcessTreeTerminator(),
    log: appLog.write,
    onFinished: info => {
      if (info.op !== 'run' && info.op !== 'resume') return
      lastRun = info
      notifyFinished(info, prefs.get().workDir)
    }
  })
  v1Real = real
  v1MediaRoots = () => [prefs.get().outputDir, prefs.get().workDir]
  const articles = new RoutedEngineAdapter(real, devTools ? demo : null, () => devTools ? demo.settings.mode : 'real')
  const appInfo = (): AppInfo => ({
    appVersion: app.getVersion(), platform: process.platform, isPackaged: paths.isPackaged, devTools,
    engine: describeEngine(engineLocation, engineVersion),
    adapterKinds: { article: devTools && demo.settings.mode === 'demo' ? 'demo' : 'real', keyCheck: 'real' }
  })
  const coverDir = join(paths.userDataRoot, 'covers')
  const activeArticle = (): string | null => real.activeRun()?.article ?? null
  const licenses = createLicenseCatalog(licenseRoots(), componentVersions())
  registerV1Ipc({
    window: () => mainWindow, appInfo, prefs, secrets, articles, demo: devTools ? demo : null, noPaid,
    extras: {
      runtimePrep: () => preparer?.get() ?? { status: 'not_needed' },
      retryRuntimePrep: () => preparer ? preparer.retry() : Promise.resolve({ status: 'not_needed' }),
      licenses: () => licenses.list(),
      licenseFile: id => licenses.file(id)
    },
    keyCheck: new EngineKeyCheck(() => engineLocation, () => secrets.engineEnv(), appLog.write),
    logsDir,
    data: {
      listTasks: workDir => scanTasks(workDir, activeArticle()),
      articleInfo: (workDir, article) => {
        const facts = readTaskFacts(workDir, article, article === activeArticle())
        return { article, title: facts.title, contentType: facts.contentType, source: facts.source }
      },
      listVideos: async (outputDir, workDir) => {
        const out: VideoItem[] = []
        for (const video of listVideos(outputDir)) {
          const article = videoArticle(video.file, video.article)
          const durationSec = await probeDuration(video.file, engineLocation.ffmpegDir ?? null)
          const report = readReportSummary(join(workDir, article))
          let mtime = ''
          try { mtime = (await import('node:fs')).statSync(video.file).mtime.toISOString() } catch { /* 读不到 */ }
          out.push({ file: video.file, name: video.file.split(/[\\/]/).pop() ?? video.file, article, title: readArticleTitle(join(workDir, article)) ?? article,
            durationSec, cover: await coverDataUrl(video.file, durationSec, engineLocation.ffmpegDir ?? null, coverDir), ...report, mtime })
        }
        return out.sort((a, b) => b.mtime.localeCompare(a.mtime))
      },
      diskUsage: workDir => diskUsage(workDir, activeArticle()),
      cleanupPreview: (workDir, outputDir) => cleanupPreview(workDir, activeArticle(), outputDir),
      // 移到回收站（废纸篓）；回收站不可用时 trashItem 报错，不会改成永久删除
      cleanupRun: (workDir, outputDir, names) => runCleanup(workDir, activeArticle(), outputDir, names, path => shell.trashItem(path), appLog.write),
      // 1.0.1（R101 J）：删除任务 = 工作文件夹移到回收站（trashItem 失败就报错，不会改成永久删除）
      deleteTaskCheck: (workDir, article) => deleteTaskCheck(workDir, activeArticle(), article),
      deleteTask: (workDir, article) => deleteTask(workDir, activeArticle(), article, path => shell.trashItem(path), appLog.write),
      exportDiagnostics: async target => {
        const info = appInfo()
        const reportFile = lastRun?.runDone.report_json ?? (lastRun?.article ? join(prefs.get().workDir, lastRun.article, '10-报告', '检查结果.json') : null)
        const entries = collectDiagnostics({
          info: {
            app: { version: info.appVersion, packaged: info.isPackaged, electron: process.versions.electron, node: process.versions.node },
            engine: { version: info.engine.version, status: info.engine.status, problems: info.engine.problems },
            system: { platform: process.platform, arch: process.arch, release: (await import('node:os')).release() },
            keys: Object.fromEntries(Object.entries(secrets.status()).map(([id, value]) => [id, value.filled ? '已填写' : '未填写'])),
            lastKeyCheck: prefs.get().lastKeyCheck,
            lastRun: lastRun ? { op: lastRun.op, article: lastRun.article, exitCode: lastRun.runDone.exit_code, errorKind: lastRun.runDone.error_kind ?? null } : null,
            exportedAt: new Date().toISOString()
          },
          logsDir, lastEvents: lastRun?.events ?? null, lastReportFile: reportFile
        })
        try {
          const written = writeDiagnosticsZip(target, entries, Object.values(secrets.engineEnv()))
          appLog.write(`诊断信息已导出（${written.entries.length} 项，${written.bytes} 字节）`)
          return { ok: true, file: target, ...written }
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          appLog.write(`诊断信息导出失败：${message}`)
          return { ok: false, error: message }
        }
      }
    }
  })
}

// APP3：第三方组件许可文件的位置（安装版：Runtime Pack 的 licenses/ 与安装目录；开发版：仓库与开发用运行环境）
function licenseRoots(): LicenseRoots {
  const packaged = resources && 'runtimePackRoot' in resources ? resources as PackagedResources : null
  if (paths.isPackaged && packaged) return packagedLicenseRoots(packaged.runtimePackRoot, paths.resourcesRoot)
  const repo = paths.resourcesRoot
  return { packRoot: join(repo, '.cache', 'runtime-pack-v5.7'), electronRoot: join(repo, 'node_modules', 'electron', 'dist'), repoRoot: repo, runtimeDir: engineLocation.runtimeDir ?? null, resourcesRoot: null }
}

function componentVersions(): ReturnType<typeof versionsFromAssets> {
  const read = (file: string): unknown => { try { return JSON.parse(readFileSync(file, 'utf8').replace(/^\uFEFF/, '')) } catch { return null } }
  const packaged = resources && 'manifestPath' in resources ? resources as PackagedResources : null
  if (paths.isPackaged && packaged) {
    const manifest = read(packaged.manifestPath) as { assets?: unknown } | null
    return versionsFromAssets(manifest?.assets, process.versions.electron)
  }
  const baseline = read(join(paths.resourcesRoot, 'src', 'shared', 'runtime-baseline-v5.7.json')) as { ffmpeg?: unknown } | null
  return versionsFromAssets(read(join(paths.resourcesRoot, 'scripts', 'runtime', 'assets-lock.json')), process.versions.electron, baseline?.ffmpeg)
}

// 出片完成提醒（任务书 APP2 3.4.1）：APP 不在前台时弹系统通知
function notifyFinished(info: FinishedRun, workDir: string): void {
  try {
    if (!Notification.isSupported()) return
    if (mainWindow && !mainWindow.isDestroyed() && mainWindow.isFocused()) return
    const title = (info.article ? readArticleTitle(join(workDir, info.article)) : null) ?? info.article ?? '文章'
    const done = info.runDone.exit_code === 0 || info.runDone.exit_code === 1
    const reason = done ? '' : errorGuide(info.runDone.error_kind, { service: info.runDone.service ?? null }).title.replace(/，?已暂停$/, '').replace(/，暂停$/, '')
    new Notification({ title: '文章成片', body: done ? `《${title}》出片完成` : `《${title}》已暂停：${reason}` }).show()
  } catch { /* 通知失败不影响使用 */ }
}

function registerMediaProtocol(): void {
  protocol.handle(MEDIA_SCHEME, request => {
    const file = new URL(request.url).searchParams.get('p') ?? ''
    const allowed = /\.mp4$/i.test(file) && v1MediaRoots().some(root => root && (file === root || file.startsWith(root.endsWith('/') || root.endsWith('\\') ? root : `${root}${process.platform === 'win32' ? '\\' : '/'}`)))
    if (!allowed || file.includes('..')) return new Response('forbidden', { status: 403 })
    return net.fetch(pathToFileURL(file).toString(), { headers: request.headers })
  })
}

function guardQuit(event: { preventDefault(): void }): void {
  if (!quitCoordinator || quitCoordinator.isQuitAllowed()) return
  event.preventDefault()
  void quitCoordinator.requestQuit()
}
