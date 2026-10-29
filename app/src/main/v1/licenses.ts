import { readdirSync, statSync, type Dirent } from 'node:fs'
import { dirname, join } from 'node:path'
import type { ThirdPartyComponent } from '../../shared/v1/licenses'
import type { ExternalLinkId } from '../../shared/v1/links'

// APP3：关于页"第三方组件"。安装版打开 Runtime Pack 随附的许可文件（<Pack>/licenses/…，由 runtime_pack.py
// 的 archive_licenses 归档：licenses/<python|node|runtime|ffmpeg>/<原相对路径>，字体在 licenses/fonts/）；
// Electron 的许可在安装目录（electron-builder 带的 LICENSE.electron.txt、LICENSES.chromium.html）。
// 开发版先找仓库里组装过的 .cache/runtime-pack-<版本>/licenses，再找开发用运行环境和 node_modules 里的原文件。

export interface LicenseRoots {
  // 安装版：<resources>/runtime-pack-<版本>；开发版：仓库里组装过的 .cache/runtime-pack-<版本>（可能不存在）
  packRoot: string | null
  // 安装版：Electron 安装目录（resources 的上一层）；开发版：node_modules/electron/dist
  electronRoot: string | null
  // 开发版：仓库根（node_modules）与开发用运行环境（~/dev/a2v-runtime）
  repoRoot: string | null
  runtimeDir: string | null
  // 安装版：<resources>（electron-builder extraResources 放到 resources/licenses/ 的许可文件，如 React、Lucide）
  resourcesRoot: string | null
}

export interface ComponentVersions {
  python: string
  node: string
  ffmpeg: string
  remotion: string
  browser: string
  electron: string
  wheels: { package: string; version: string; filename: string }[]
}

interface Candidate { root: keyof LicenseRoots; rel: string; folder?: boolean }

interface Definition extends Omit<ThirdPartyComponent, 'available'> { candidates: Candidate[] }

const LICENSE_NAME = /^(license|licence|copying|notice|ofl)([.\-_]|$)/i
const WHEEL_LICENSES: Record<string, string> = {
  certifi: 'MPL-2.0', 'charset-normalizer': 'MIT', idna: 'BSD-3-Clause', numpy: 'BSD-3-Clause',
  Pillow: 'MIT-CMU（HPND）', requests: 'Apache-2.0', urllib3: 'MIT'
}

const pack = (rel: string, folder = false): Candidate => ({ root: 'packRoot', rel: `licenses/${rel}`, folder })

export function componentDefinitions(v: ComponentVersions): Definition[] {
  const browserDir = 'node_modules/.remotion/chrome-headless-shell'
  return [
    { id: 'ffmpeg', name: 'FFmpeg', version: v.ffmpeg, license: 'LGPL-2.1-or-later', candidates: [pack('ffmpeg')] },
    { id: 'remotion', name: 'Remotion', version: v.remotion, license: 'Remotion License', officialLink: 'remotionLicense',
      candidates: [pack('runtime/node_modules/remotion'), { root: 'runtimeDir', rel: 'node_modules/remotion' }] },
    { id: 'chromium', name: 'Chromium headless shell', version: v.browser, license: 'BSD-3-Clause 等（见许可文件）',
      candidates: [pack(`runtime/${browserDir}`), { root: 'runtimeDir', rel: browserDir }] },
    { id: 'node', name: 'Node.js', version: v.node, license: 'MIT', candidates: [pack('node')] },
    { id: 'python', name: 'Python', version: v.python, license: 'PSF-2.0', candidates: [pack('python', false)] },
    ...v.wheels.map(w => ({ id: `py-${w.package}`, name: `Python 包 ${w.package}`, version: w.version, license: WHEEL_LICENSES[w.package] ?? '见许可文件',
      candidates: [{ root: 'packRoot' as const, rel: `licenses/python/Lib/site-packages/${wheelInfoDir(w)}` }] })),
    { id: 'font', name: '霞鹜文楷 LXGW WenKai（随包字体）', version: '1.522', license: 'SIL Open Font License 1.1',
      candidates: [pack('fonts/LXGW-WenKai/OFL.txt')] },
    { id: 'electron', name: 'Electron', version: v.electron, license: 'MIT',
      candidates: [{ root: 'electronRoot', rel: 'LICENSE.electron.txt' }, { root: 'electronRoot', rel: 'LICENSE' }] },
    { id: 'chromium-electron', name: 'Chromium（Electron 内置）', version: v.electron, license: '多种开源许可（见许可文件）',
      candidates: [{ root: 'electronRoot', rel: 'LICENSES.chromium.html' }] },
    { id: 'npm-runtime', name: 'Remotion 运行所需的其他 npm 包', version: '—', license: '各自的许可（见许可文件夹）',
      candidates: [pack('runtime/node_modules', true), { root: 'runtimeDir', rel: 'node_modules', folder: true }] },
    { id: 'react', name: 'React', version: '19.3.0', license: 'MIT', candidates: [{ root: 'repoRoot', rel: 'node_modules/react/LICENSE' }, { root: 'resourcesRoot', rel: 'licenses/react/LICENSE' }] },
    { id: 'lucide', name: 'Lucide 图标（lucide-react）', version: '1.49.0', license: 'ISC', candidates: [{ root: 'repoRoot', rel: 'node_modules/lucide-react/LICENSE' }, { root: 'resourcesRoot', rel: 'licenses/lucide-react/LICENSE' }] }
  ]
}

// wheel 文件名的前两段就是安装后的 dist-info 目录名：pillow-12.3.0-cp312-….whl → pillow-12.3.0.dist-info
export function wheelInfoDir(w: { filename: string }): string {
  const [name, version] = w.filename.split('-')
  return `${name}-${version}.dist-info`
}

// 版本号来自资产锁定（安装版读 Runtime Pack 的 manifest/runtime-pack.json 里的 assets；
// 开发版读仓库的 scripts/runtime/assets-lock.json，FFmpeg 用 v5.7 档案覆盖的那份）。读不到的写"—"。
export function versionsFromAssets(assets: unknown, electron: string, ffmpegOverride?: unknown): ComponentVersions {
  const a = (assets && typeof assets === 'object' ? assets : {}) as Record<string, any>
  const text = (value: unknown): string => typeof value === 'string' && value ? value : '—'
  const ffmpeg = (ffmpegOverride && typeof ffmpegOverride === 'object' ? ffmpegOverride : a.ffmpeg) as Record<string, unknown> | undefined
  return {
    python: text(a.python?.version), node: text(a.node?.version), ffmpeg: text(ffmpeg?.version),
    remotion: text(a.runtime?.remotion), browser: text(a.runtime?.browser), electron,
    wheels: Array.isArray(a.wheels) ? a.wheels.filter((w: any) => typeof w?.package === 'string' && typeof w?.filename === 'string')
      .map((w: any) => ({ package: w.package, version: text(w.version), filename: w.filename })) : []
  }
}

const isFile = (path: string): boolean => { try { return statSync(path).isFile() } catch { return false } }
const isDir = (path: string): boolean => { try { return statSync(path).isDirectory() } catch { return false } }

// 目录里找许可文件：最浅的那个，同层按名字排（LICENSE 优先于 NOTICE）
export function findLicenseFile(dir: string, depth = 4): string | null {
  let level = [dir]
  for (let i = 0; i <= depth && level.length; i++) {
    const files: string[] = []
    const next: string[] = []
    for (const current of level) {
      let entries: Dirent[]
      try { entries = readdirSync(current, { withFileTypes: true }) } catch { continue }
      for (const entry of entries) {
        if (entry.isFile() && LICENSE_NAME.test(entry.name)) files.push(join(current, entry.name))
        else if (entry.isDirectory() && entry.name !== 'node_modules') next.push(join(current, entry.name))
      }
    }
    if (files.length) return files.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))[0]!
    level = next.sort()
  }
  return null
}

const rank = (file: string): number => /(^|[\\/])(license|licence)[^\\/]*$/i.test(file) ? 0 : /copying/i.test(file) ? 1 : 2

export function resolveLicense(candidates: Candidate[], roots: LicenseRoots): string | null {
  for (const candidate of candidates) {
    const root = roots[candidate.root]
    if (!root) continue
    const path = join(root, ...candidate.rel.split('/'))
    if (candidate.folder) { if (isDir(path)) return path; continue }
    if (isFile(path)) return path
    if (isDir(path)) { const found = findLicenseFile(path); if (found) return found }
  }
  return null
}

export function createLicenseCatalog(roots: LicenseRoots, versions: ComponentVersions): { list(): ThirdPartyComponent[]; file(id: string): string | null } {
  const definitions = componentDefinitions(versions)
  return {
    list: () => definitions.map(({ candidates, ...rest }) => ({ ...rest, available: resolveLicense(candidates, roots) !== null })),
    file: id => {
      const definition = definitions.find(item => item.id === id)
      return definition ? resolveLicense(definition.candidates, roots) : null
    }
  }
}

export function packagedLicenseRoots(runtimePackRoot: string, resourcesRoot: string): LicenseRoots {
  return { packRoot: runtimePackRoot, electronRoot: dirname(resourcesRoot), repoRoot: null, runtimeDir: null, resourcesRoot }
}
