// RunningHub 注册入口。界面上所有"去注册"链接都只用这一个常量，由 Main 用系统浏览器打开。
export const RUNNINGHUB_SIGNUP_URL = 'https://www.runninghub.ai?inviteCode=rh-v1419'

// Main 只允许用系统浏览器打开这里列出的地址，页面不能传任意网址。
export const EXTERNAL_LINKS = {
  runninghubSignup: RUNNINGHUB_SIGNUP_URL,
  // APP3：关于页"第三方组件"里 Remotion 的官方许可
  remotionLicense: 'https://www.remotion.dev/license',
  // APP4：密钥行的"去哪里拿"——各家官方控制台里获取密钥的页面（地址出处见 APP4 回执）
  deepseekKeys: 'https://platform.deepseek.com/api_keys',
  ttsKeys: 'https://console.volcengine.com/speech/new/setting/apikeys',
  arkKeys: 'https://ark.volcengine.com/region:cn-beijing/apiKey',
  feishuApps: 'https://open.feishu.cn/app'
} as const

export type ExternalLinkId = keyof typeof EXTERNAL_LINKS

// R121M5：APP 内自动更新。更新源固定为下面这个 GitHub 仓库的正式 Release（electron-builder 打包时写进安装目录的
// resources/app-update.yml，electron-builder-v5.7.yml 的 publish 一节要和这里一致）。
// 自动更新失败时给两个手动下载地址；Main 只肯打开这两个前缀下的地址，其他一律不打开。
export const UPDATE_REPO = { owner: 'Chopper888', repo: 'qb-article2video' } as const
export const GITHUB_RELEASES_URL = `https://github.com/${UPDATE_REPO.owner}/${UPDATE_REPO.repo}/releases`
export const QUARK_PREFIX = 'https://pan.quark.cn/'
export const QUARK_DOWNLOAD_URL = 'https://pan.quark.cn/s/03ebea3c47ef'
export const UPDATE_LINK_PREFIXES = [GITHUB_RELEASES_URL, QUARK_PREFIX] as const

// 只放行 https、没有用户名密码、规范化后以白名单前缀开头的地址。
// GitHub 前缀后面必须紧跟 / ? # 或结束（挡住 …/releases-evil 这类同前缀的别的路径）。
export function isAllowedUpdateLink(raw: unknown): boolean {
  if (typeof raw !== 'string' || raw.length > 2048 || /[\s"'<>\\]/.test(raw)) return false
  let url: URL
  try { url = new URL(raw) } catch { return false }
  if (url.protocol !== 'https:' || url.username || url.password) return false
  const href = url.href
  if (href.startsWith(QUARK_PREFIX)) return url.hostname === 'pan.quark.cn' && href.length > QUARK_PREFIX.length
  if (href.startsWith(GITHUB_RELEASES_URL)) {
    const next = href.charAt(GITHUB_RELEASES_URL.length)
    return url.hostname === 'github.com' && (next === '' || next === '/' || next === '?' || next === '#')
  }
  return false
}
