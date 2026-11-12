import { notesPreview } from './update-check'

// R121M5：APP 内自动更新的状态（Main 维护，推给页面；页面只按这个状态画提示条 / 小窗口 / 关于页）。
// 检查、下载、校验、安装都在 Main（electron-updater）；页面不知道任何下载地址。

export type UpdatePhase =
  | 'idle'          // 还没检查过（或自动检查没查到 / 连不上：不提示）
  | 'checking'
  | 'latest'        // 已是最新
  | 'available'     // 发现新版本，等用户点"立即更新"
  | 'downloading'
  | 'downloaded'    // 已下载，等"重启安装"或关闭软件时安装
  | 'failed'
  | 'unsupported'   // 开发模式 / 没有更新配置：不检查

// check = 连不上更新服务器；download = 下载没完成；verify = 文件校验没通过；install = 没装上
export type UpdateFailStage = 'check' | 'download' | 'verify' | 'install'

export interface UpdateState {
  phase: UpdatePhase
  currentVersion: string
  // 发现的 / 正在下载的 / 已下载的版本
  version: string | null
  // 更新内容（纯文字，最多 NOTES_MAX_LINES 行）
  notes: string[]
  percent: number
  transferredBytes: number
  totalBytes: number
  failStage: UpdateFailStage | null
  // 这个版本被"跳过"了（自动检查不提示；手动检查照常显示）
  skipped: boolean
  // 本次运行点过"以后再说"（或关掉了失败提示）
  dismissed: boolean
  // 最近一次检查是不是在"关于"页手动点的
  manual: boolean
  // 正在出片：不能"重启安装"
  busy: boolean
  // 已下载时：关闭软件会自动安装
  installOnQuit: boolean
  lastCheckedAt: string | null
  // 更新源被环境变量 A2V_UPDATE_FEED_URL 改过（只给测试用）
  feedOverridden: boolean
}

export const NOTES_MAX_LINES = 12

export function initialUpdateState(currentVersion: string, phase: UpdatePhase = 'idle'): UpdateState {
  return { phase, currentVersion, version: null, notes: [], percent: 0, transferredBytes: 0, totalBytes: 0, failStage: null,
    skipped: false, dismissed: false, manual: false, busy: false, installOnQuit: false, lastCheckedAt: null, feedOverridden: false }
}

export const FAIL_TEXT: Record<UpdateFailStage, string> = {
  check: '暂时连不上更新服务器',
  download: '新版本没有下载下来',
  verify: '下载的文件校验没通过',
  install: '新版本没有装上'
}

// 顶部提示条显示哪一种（null = 不显示）
export type UpdateBarKind = 'available' | 'downloading' | 'downloaded' | 'failed'
export function updateBarKind(state: UpdateState | null): UpdateBarKind | null {
  if (!state) return null
  switch (state.phase) {
    case 'available': return state.dismissed || (state.skipped && !state.manual) ? null : 'available'
    case 'downloading': return 'downloading'
    case 'downloaded': return 'downloaded'
    // 连不上更新服务器不占提示条：自动检查时什么都不显示，手动检查时在"关于"页里说
    case 'failed': return state.failStage === 'check' || state.dismissed ? null : 'failed'
    default: return null
  }
}

// "重启安装"按钮：出片中置灰
export function installButton(state: UpdateState): { disabled: boolean; text: string } {
  return state.busy ? { disabled: true, text: '出片结束后可安装' } : { disabled: false, text: '重启安装' }
}

export function bytesText(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB'
  return bytes >= 1024 * 1024 * 1024 ? `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB` : `${(bytes / 1024 / 1024).toFixed(bytes >= 100 * 1024 * 1024 ? 0 : 1)} MB`
}

const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', '#39': "'" }

// 更新内容 → 纯文字行。electron-updater 给的可能是 GitHub 发布说明的 HTML（字符串），也可能是 [{version, note}]，也可能没有。
// 一律去掉标签、链接地址，只留文字；页面按纯文字显示，不渲染 HTML。
export function cleanReleaseNotes(raw: unknown, lines = NOTES_MAX_LINES): string[] {
  let text = ''
  if (typeof raw === 'string') text = raw
  else if (Array.isArray(raw)) text = raw.map(item => item && typeof item === 'object' && typeof (item as { note?: unknown }).note === 'string' ? (item as { note: string }).note : '').join('\n')
  if (!text) return []
  text = text.slice(0, 100_000)
    .replace(/<(script|style)\b[\s\S]*?<\/\1>/gi, '')
    .replace(/<\s*(br|\/p|\/li|\/h[1-6]|\/div|\/tr)\s*\/?>/gi, '\n')
    .replace(/<[^>]*>/g, '')
    .replace(/&(#39|[a-z]+);/gi, (all, name: string) => ENTITIES[name.toLowerCase()] ?? all)
  return notesPreview(text, lines)
}
