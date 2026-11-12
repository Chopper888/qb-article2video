import type { UpdaterBackend } from './auto-updater'

// R121M5：安装版的更新后端 = electron-updater（与 electron-builder 的 NSIS 安装包配套）。
// 只在安装版、第一次检查更新时才装载这个包（开发模式和测试都不碰它）。
//
// 设置：
// - autoDownload = false：发现新版本不自动下载，等用户点"立即更新"。
// - autoInstallOnAppQuit = true：下载好之后，软件正常退出时静默安装（不重新打开）。
// - allowPrerelease = false / allowDowngrade = false：只认 GitHub 上的正式 Release（/releases/latest 不含草稿和预发布），不降级。
// - 不设置发布者校验（安装包没有代码签名）；下载完成后 electron-updater 按 latest.yml 里的 sha512 校验文件，不一致就报错。
// - feedUrl：只来自环境变量 A2V_UPDATE_FEED_URL（测试用），换成 generic 更新源；平时用安装目录里的 resources/app-update.yml。

export interface ElectronUpdaterOptions {
  log(line: string): void
  feedUrl?: string | null
}

interface UpdaterLike {
  autoDownload: boolean
  autoInstallOnAppQuit: boolean
  autoRunAppAfterInstall: boolean
  allowPrerelease: boolean
  allowDowngrade: boolean
  logger: unknown
  on(event: string, listener: (...args: never[]) => void): unknown
  removeListener(event: string, listener: (...args: never[]) => void): unknown
  setFeedURL(options: { provider: 'generic'; url: string }): void
  checkForUpdates(): Promise<{ isUpdateAvailable?: boolean; updateInfo?: { version?: string; releaseNotes?: unknown } } | null>
  downloadUpdate(): Promise<unknown>
  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void
}

export function configureUpdater(updater: UpdaterLike, options: ElectronUpdaterOptions): UpdaterBackend {
  updater.autoDownload = false
  updater.autoInstallOnAppQuit = true
  updater.autoRunAppAfterInstall = true
  updater.allowPrerelease = false
  updater.allowDowngrade = false
  const line = (level: string) => (message: unknown): void => options.log(`自动更新[${level}]：${String(message instanceof Error ? message.message : message).split(/\r?\n/)[0]!.slice(0, 300)}`)
  updater.logger = { info: line('info'), warn: line('warn'), error: line('error'), debug: () => undefined }
  // 出错时 electron-updater 既 reject 又发 error 事件；不接这个事件会变成未处理的异常
  updater.on('error', () => undefined)
  if (options.feedUrl) updater.setFeedURL({ provider: 'generic', url: options.feedUrl })
  return {
    async check() {
      const result = await updater.checkForUpdates()
      if (!result) throw new Error('这个版本没有更新配置')
      return { available: result.isUpdateAvailable === true, version: String(result.updateInfo?.version ?? ''), notes: result.updateInfo?.releaseNotes }
    },
    async download(onProgress) {
      const listener = (progress: { percent?: number; transferred?: number; total?: number }): void =>
        onProgress({ percent: Number(progress?.percent) || 0, transferred: Number(progress?.transferred) || 0, total: Number(progress?.total) || 0 })
      updater.on('download-progress', listener as never)
      try { await updater.downloadUpdate() } finally { updater.removeListener('download-progress', listener as never) }
    },
    // 静默安装，装完重新打开软件
    quitAndInstall() { updater.quitAndInstall(true, true) }
  }
}

export async function loadElectronUpdaterBackend(options: ElectronUpdaterOptions): Promise<UpdaterBackend> {
  const module = await import('electron-updater')
  const updater = (module.autoUpdater ?? (module as unknown as { default: { autoUpdater: unknown } }).default.autoUpdater) as unknown as UpdaterLike
  return configureUpdater(updater, options)
}
