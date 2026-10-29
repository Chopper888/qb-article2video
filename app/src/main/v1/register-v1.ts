import { dialog, ipcMain, shell, type BrowserWindow, type IpcMainInvokeEvent } from 'electron'
import { mkdirSync, readFileSync, statSync } from 'node:fs'
import { isAbsolute, join } from 'node:path'
import type { ArticleEngineAdapter, EngineSession, KeyCheckAdapter } from '../../shared/v1/adapter'
import { DEMO_IMPORT_SCENARIOS, DEMO_RUN_SCENARIOS } from '../../shared/v1/adapter'
import { IPC_V1 } from '../../shared/v1/ipc'
import { KEY_FIELD_IDS, type KeyFieldId } from '../../shared/v1/keys'
import { EXTERNAL_LINKS, type ExternalLinkId } from '../../shared/v1/links'
import { parseChapters } from '../../shared/v1/chapters'
import type { CleanupPreview, CleanupResult, DiskUsageReal, TaskRow, VideoItem } from '../../shared/v1/tasks'
import type { ThirdPartyComponent } from '../../shared/v1/licenses'
import type { AppInfo, ArticleInfo, DeleteTaskOutcome, DemoState, ExportResult, RuntimePrep, V1Prefs } from '../../shared/v1/types'
import { checkArticleName, normalizeImportInput } from './real-engine-adapter'
import type { DemoEngineAdapter } from './demo-engine-adapter'
import type { DevNoPaid } from './dev-no-paid'
import { restageDroppedFiles, stageDroppedFiles } from './drop-import'
import { DEMO_CHAPTERS } from './demo-scenarios'
import type { PrefsStore } from './prefs-store'
import type { SecretStore } from './secret-store'

export interface V1Context {
  window: () => BrowserWindow | null
  appInfo: () => AppInfo
  prefs: PrefsStore
  secrets: SecretStore
  articles: ArticleEngineAdapter
  keyCheck: KeyCheckAdapter
  demo: DemoEngineAdapter | null
  // APP4：开发者开关"不付费试跑"（只在内存里；非开发者模式时永远是关的）
  noPaid?: DevNoPaid
  logsDir: string
  // APP2：真实数据（任务列表、成片库、磁盘占用、诊断导出）
  data?: V1DataSources
  extras?: V1Extras
}

export interface V1DataSources {
  listTasks(workDir: string): Promise<TaskRow[]> | TaskRow[]
  listVideos(outputDir: string, workDir: string): Promise<VideoItem[]>
  diskUsage(workDir: string): Promise<DiskUsageReal> | DiskUsageReal
  cleanupPreview(workDir: string, outputDir: string): Promise<CleanupPreview> | CleanupPreview
  // APP3：清理确认框里确认过的文章（移到回收站）
  cleanupRun(workDir: string, outputDir: string, articles: string[]): Promise<CleanupResult>
  // 1.0.1（R101 J）：删除任务（工作文件夹移到回收站）
  deleteTaskCheck?(workDir: string, article: string): DeleteTaskOutcome
  deleteTask?(workDir: string, article: string): Promise<DeleteTaskOutcome>
  exportDiagnostics(target: string): Promise<ExportResult>
  // APP4：一篇文章的标题和类型（完成页、"重新出片"用）
  articleInfo?(workDir: string, article: string): ArticleInfo | null
}

// APP3：首次启动准备运行环境、第三方组件许可
export interface V1Extras {
  runtimePrep(): RuntimePrep
  retryRuntimePrep(): Promise<RuntimePrep>
  licenses(): ThirdPartyComponent[]
  licenseFile(id: string): string | null
}

export function registerV1Ipc(ctx: V1Context): void {
  const trusted = (event: IpcMainInvokeEvent): BrowserWindow => {
    const window = ctx.window()
    if (!window || event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) {
      throw new Error('拒绝未知窗口的 IPC 请求')
    }
    return window
  }
  const send = (channel: string, payload: unknown): void => {
    const window = ctx.window()
    if (window && !window.isDestroyed()) window.webContents.send(channel, payload)
  }
  const forward = (start: (sink: (event: unknown) => void) => EngineSession): string => {
    let id = ''
    const pending: unknown[] = []
    const session = start(event => { if (id) send(IPC_V1.sessionEvent, { sessionId: id, event }); else pending.push(event) })
    id = session.sessionId
    for (const event of pending) send(IPC_V1.sessionEvent, { sessionId: id, event })
    void session.done.then(exitCode => send(IPC_V1.sessionEnd, { sessionId: id, exitCode }))
    return id
  }
  const text = (value: unknown, name: string): string => {
    if (typeof value !== 'string' || !value.trim() || value.length > 4096) throw new Error(`${name}不对`)
    return value.trim()
  }
  const cost = (value: unknown): number => {
    if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0 || value > 10000) throw new Error('费用上限不对')
    return value
  }

  // APP4：设置一变就推给页面（不管是谁改的），确认页、成片库、任务列表立刻用新值
  ctx.prefs.subscribe?.(prefs => send(IPC_V1.prefsChanged, prefs))

  ipcMain.handle(IPC_V1.getAppInfo, event => { trusted(event); return ctx.appInfo() })
  ipcMain.handle(IPC_V1.getPrefs, event => { trusted(event); return ctx.prefs.get() })
  ipcMain.handle(IPC_V1.updatePrefs, (event, patch: Partial<V1Prefs>) => {
    trusted(event)
    const { lastKeyCheck: _ignored, ...rest } = (patch ?? {}) as V1Prefs
    return ctx.prefs.update(rest)
  })
  ipcMain.handle(IPC_V1.getKeyStatus, event => { trusted(event); return ctx.secrets.status() })
  ipcMain.handle(IPC_V1.setKey, (event, id: unknown, value: unknown) => {
    trusted(event)
    if (typeof id !== 'string' || !KEY_FIELD_IDS.includes(id as KeyFieldId)) throw new Error('未知的密钥项')
    return ctx.secrets.set(id as KeyFieldId, typeof value === 'string' ? value : '')
  })
  ipcMain.handle(IPC_V1.checkKeys, async event => {
    trusted(event)
    const outcome = await ctx.keyCheck.checkKeys()
    if (outcome.results.length) {
      ctx.prefs.recordKeyCheck({ checkedAt: outcome.checkedAt, statuses: Object.fromEntries(outcome.results.map(r => [r.service, r.status])) })
    }
    return outcome
  })
  ipcMain.handle(IPC_V1.openExternal, async (event, link: unknown) => {
    trusted(event)
    if (typeof link !== 'string' || !(link in EXTERNAL_LINKS)) throw new Error('不允许打开这个地址')
    await shell.openExternal(EXTERNAL_LINKS[link as ExternalLinkId])
  })
  ipcMain.handle(IPC_V1.openFolder, async (event, target: unknown) => {
    trusted(event)
    const prefs = ctx.prefs.get()
    const dir = target === 'work' ? prefs.workDir : target === 'output' ? prefs.outputDir : target === 'logs' ? ctx.logsDir : null
    if (!dir) throw new Error('未知的文件夹')
    mkdirSync(dir, { recursive: true })
    const error = await shell.openPath(dir)
    if (error) throw new Error(`打不开文件夹：${error}`)
  })
  ipcMain.handle(IPC_V1.chooseFolder, async (event, target: unknown) => {
    const window = trusted(event)
    if (target !== 'work' && target !== 'output') throw new Error('未知的文件夹')
    const prefs = ctx.prefs.get()
    const result = await dialog.showOpenDialog(window, { properties: ['openDirectory', 'createDirectory'],
      defaultPath: target === 'work' ? prefs.workDir : prefs.outputDir, title: target === 'work' ? '选择工作文件夹' : '选择成片保存位置' })
    if (result.canceled || !result.filePaths[0]) return prefs
    return ctx.prefs.update(target === 'work' ? { workDir: result.filePaths[0] } : { outputDir: result.filePaths[0] })
  })
  const workRoot = (): string => ctx.prefs.get().workDir
  // 本次运行里导入过的本地来源（文件夹 / zip / md）：问题页"打开文章文件夹"只能打开这些
  const importedSources = new Set<string>()
  // 1.0.1（R101 M）：拖进多个文件时复制出来的文件夹 → 用户的原始文件。"重新检查"按原始文件重新复制
  const droppedSources = new Map<string, string[]>()
  ipcMain.handle(IPC_V1.importArticle, (event, input: unknown) => {
    trusted(event)
    const value = normalizeImportInput(text(input, '文章链接或文件夹'))
    const originals = droppedSources.get(value)
    if (originals) restageDroppedFiles(originals, value)
    if (isAbsolute(value)) importedSources.add(value)
    return forward(sink => ctx.articles.importArticle({ input: value, workRoot: workRoot() }, sink))
  })
  // APP4：一次拖进多个文件（一个 md 加图片）：复制进 <工作文件夹>/_a2v_import/drop-…/ 再按文件夹导入
  ipcMain.handle(IPC_V1.importDropped, (event, paths: unknown) => {
    trusted(event)
    if (!Array.isArray(paths) || !paths.length) throw new Error('没有拖进来文件')
    const list = paths.map(item => text(item, '拖进来的文件'))
    const input = list.length === 1 ? normalizeImportInput(list[0]!) : stageDroppedFiles(list, workRoot())
    if (list.length > 1) droppedSources.set(input, list)
    importedSources.add(input)
    return { sessionId: forward(sink => ctx.articles.importArticle({ input, workRoot: workRoot() }, sink)), input }
  })
  ipcMain.handle(IPC_V1.chooseImportFolder, async event => {
    const window = trusted(event)
    const result = await dialog.showOpenDialog(window, { properties: ['openDirectory'], title: '选择文章文件夹' })
    return result.canceled || !result.filePaths[0] ? null : result.filePaths[0]
  })
  // APP4：导入本地文章。Mac 上一个选择框能同时选文件夹和 .zip / .md；Windows 上做不到，页面分成两个入口
  ipcMain.handle(IPC_V1.chooseImportSource, async (event, kind: unknown) => {
    const window = trusted(event)
    if (kind !== 'any' && kind !== 'folder' && kind !== 'file') throw new Error('未知的选择方式')
    const properties: ('openFile' | 'openDirectory')[] = kind === 'folder' ? ['openDirectory'] : kind === 'file' ? ['openFile'] : ['openFile', 'openDirectory']
    const result = await dialog.showOpenDialog(window, {
      properties, title: kind === 'folder' ? '选择文章文件夹' : '选择文章（文件夹、.zip 或 .md）',
      ...(kind === 'folder' ? {} : { filters: [{ name: '文章（.zip、.md）', extensions: ['zip', 'md'] }] })
    })
    return result.canceled || !result.filePaths[0] ? null : result.filePaths[0]
  })
  ipcMain.handle(IPC_V1.openArticleSource, async (event, input: unknown) => {
    trusted(event)
    const value = normalizeImportInput(text(input, '文章文件夹'))
    // 本地来源：文件夹直接打开，zip / md 在所在文件夹里选中；飞书链接：打开工作文件夹
    if (importedSources.has(value)) {
      let isFile = false
      try { isFile = statSync(value).isFile() } catch { /* 不在了 */ }
      if (isFile) { shell.showItemInFolder(value); return }
      const error = await shell.openPath(value)
      if (error) throw new Error(`打不开文件夹：${error}`)
      return
    }
    const dir = workRoot()
    mkdirSync(dir, { recursive: true })
    const error = await shell.openPath(dir)
    if (error) throw new Error(`打不开文件夹：${error}`)
  })
  ipcMain.handle(IPC_V1.articleInfo, (event, article: unknown) => {
    trusted(event)
    const name = checkArticleName(text(article, '文章名'))
    return ctx.data?.articleInfo ? ctx.data.articleInfo(workRoot(), name) : null
  })
  ipcMain.handle(IPC_V1.reloadWindow, event => { trusted(event).webContents.reload() })
  ipcMain.handle(IPC_V1.openDevTools, event => {
    const window = trusted(event)
    if (!ctx.appInfo().devTools) throw new Error('只有开发者模式能打开开发者工具')
    window.webContents.openDevTools({ mode: 'detach' })
  })
  ipcMain.handle(IPC_V1.readChapters, (event, article: unknown) => {
    trusted(event)
    const name = checkArticleName(text(article, '文章名'))
    if (ctx.demo?.settings.mode === 'demo') return DEMO_CHAPTERS
    try {
      const file = join(workRoot(), name, '00-输入', '文章.md')
      if (statSync(file).size > 20 * 1024 * 1024) return null
      return parseChapters(readFileSync(file, 'utf8'))
    } catch { return null }
  })
  ipcMain.handle(IPC_V1.listTasks, async event => { trusted(event); return ctx.data ? ctx.data.listTasks(workRoot()) : [] })
  ipcMain.handle(IPC_V1.listVideos, async event => { trusted(event); return ctx.data ? ctx.data.listVideos(ctx.prefs.get().outputDir, workRoot()) : [] })
  ipcMain.handle(IPC_V1.diskUsage, async event => { trusted(event); return ctx.data ? ctx.data.diskUsage(workRoot()) : { completed: 0, unfinished: 0, cache: 0, total: 0 } })
  ipcMain.handle(IPC_V1.cleanupPreview, async event => { trusted(event); return ctx.data ? ctx.data.cleanupPreview(workRoot(), ctx.prefs.get().outputDir) : { articles: [], totalBytes: 0 } })
  ipcMain.handle(IPC_V1.cleanupRun, async (event, articles: unknown): Promise<CleanupResult> => {
    trusted(event)
    if (!Array.isArray(articles) || articles.length > 1000) throw new Error('要清理的文章不对')
    const names = articles.map(item => checkArticleName(text(item, '文章名')))
    if (!ctx.data) return { ok: false, articles: [], trashed: 0, bytes: 0, error: '这个版本不支持清理' }
    return ctx.data.cleanupRun(workRoot(), ctx.prefs.get().outputDir, names)
  })
  ipcMain.handle(IPC_V1.deleteTaskCheck, (event, article: unknown): DeleteTaskOutcome => {
    trusted(event)
    const name = checkArticleName(text(article, '文章名'))
    if (!ctx.data?.deleteTaskCheck) return { ok: false, article: name, error: '这个版本不支持删除任务' }
    return ctx.data.deleteTaskCheck(workRoot(), name)
  })
  ipcMain.handle(IPC_V1.deleteTask, async (event, article: unknown): Promise<DeleteTaskOutcome> => {
    trusted(event)
    const name = checkArticleName(text(article, '文章名'))
    if (!ctx.data?.deleteTask) return { ok: false, article: name, error: '这个版本不支持删除任务' }
    return ctx.data.deleteTask(workRoot(), name)
  })
  ipcMain.handle(IPC_V1.getRuntimePrep, event => { trusted(event); return ctx.extras ? ctx.extras.runtimePrep() : { status: 'not_needed' } })
  ipcMain.handle(IPC_V1.retryRuntimePrep, async event => { trusted(event); return ctx.extras ? ctx.extras.retryRuntimePrep() : { status: 'not_needed' } })
  ipcMain.handle(IPC_V1.listLicenses, event => { trusted(event); return ctx.extras ? ctx.extras.licenses() : [] })
  ipcMain.handle(IPC_V1.openLicense, async (event, id: unknown) => {
    trusted(event)
    const file = ctx.extras && typeof id === 'string' ? ctx.extras.licenseFile(id) : null
    if (!file) throw new Error('找不到这个许可文件')
    const error = await shell.openPath(file)
    if (error) throw new Error(`打不开许可文件：${error}`)
  })
  ipcMain.handle(IPC_V1.exportDiagnostics, async (event): Promise<ExportResult> => {
    const window = trusted(event)
    if (!ctx.data) return { ok: false, error: '这个版本不支持导出' }
    // 开发截图用：开发模式下 A2V_DEV_DIAG_TARGET 指定保存位置时不弹对话框
    const devTarget = ctx.appInfo().devTools && !ctx.appInfo().isPackaged ? process.env.A2V_DEV_DIAG_TARGET : undefined
    if (devTarget && isAbsolute(devTarget)) return ctx.data.exportDiagnostics(devTarget)
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    const result = await dialog.showSaveDialog(window, { title: '导出诊断信息', defaultPath: `文章成片-诊断-${stamp}.zip`, filters: [{ name: 'zip', extensions: ['zip'] }] })
    if (result.canceled || !result.filePath) return { ok: false, canceled: true }
    return ctx.data.exportDiagnostics(result.filePath)
  })
  ipcMain.handle(IPC_V1.estimate, (event, article: unknown) => {
    trusted(event)
    const name = text(article, '文章名')
    return forward(sink => ctx.articles.estimate({ article: name, workRoot: workRoot() }, sink))
  })
  ipcMain.handle(IPC_V1.run, (event, article: unknown, maxCost: unknown) => {
    trusted(event)
    const name = text(article, '文章名')
    return forward(sink => ctx.articles.run({ article: name, workRoot: workRoot(), maxCostCny: cost(maxCost) }, sink))
  })
  ipcMain.handle(IPC_V1.resume, (event, article: unknown, maxCost: unknown) => {
    trusted(event)
    const name = text(article, '文章名')
    return forward(sink => ctx.articles.resume({ article: name, workRoot: workRoot(), maxCostCny: cost(maxCost) }, sink))
  })
  ipcMain.handle(IPC_V1.stop, (event, sessionId: unknown) => { trusted(event); return ctx.articles.stop(text(sessionId, '任务')) })
  ipcMain.handle(IPC_V1.readReport, (event, article: unknown) => { trusted(event); return ctx.articles.readReport({ article: text(article, '文章名'), workRoot: workRoot() }) })

  const demoState = (): DemoState | null => ctx.demo ? { ...ctx.demo.settings, noPaid: ctx.noPaid?.enabled ?? false } : null
  ipcMain.handle(IPC_V1.getDemoState, event => { trusted(event); return ctx.appInfo().devTools ? demoState() : null })
  ipcMain.handle(IPC_V1.setDemoState, (event, patch: Partial<DemoState>) => {
    trusted(event)
    if (!ctx.appInfo().devTools || !ctx.demo) return null
    if (patch?.importScenario && (DEMO_IMPORT_SCENARIOS as readonly string[]).includes(patch.importScenario)) ctx.demo.settings.importScenario = patch.importScenario
    if (patch?.runScenario && (DEMO_RUN_SCENARIOS as readonly string[]).includes(patch.runScenario)) ctx.demo.settings.runScenario = patch.runScenario
    if (typeof patch?.delayMs === 'number' && patch.delayMs >= 0 && patch.delayMs <= 5000) ctx.demo.settings.delayMs = patch.delayMs
    if (patch?.mode === 'real' || patch?.mode === 'demo') ctx.demo.settings.mode = patch.mode
    if (typeof patch?.noPaid === 'boolean') ctx.noPaid?.set(patch.noPaid)
    return demoState()
  })
}
