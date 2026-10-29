import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { isAbsolute, join } from 'node:path'
import type { EngineInfo } from '../../shared/v1/types'

// 引擎目录和 Python 的位置。开发模式可以配置（任务书 4.4）：
//   1. 环境变量 A2V_ENGINE_ROOT / A2V_ENGINE_PYTHON；
//   2. 仓库里被 git 忽略的 .cache/a2v-engine.local.json：{"engineRoot": "...", "python": "..."}；
//   3. Mac 开发默认：~/dev/a2v-engine-p32（APP4 起用 p32-import 分支的工作副本）与 ~/dev/a2v-venv/bin/python。
// 运行环境（APP2）：A2V_RUNTIME_DIR / A2V_FFMPEG_DIR，或配置文件的 runtimeDir / ffmpegDir；
//   Mac 默认 ~/dev/a2v-runtime 与 ~/dev/a2v-runtime/bin（Remotion 自带 ffmpeg 的包装脚本）。
// 安装版用 Main 已解析的打包资源（本轮不打包，不改那一套）。

export interface EngineLocation {
  root: string | null
  python: string | null
  // APP2：run 的 --runtime-dir（node_modules + 字体）和 ffmpeg 所在目录（启动引擎时放到 PATH 最前面）
  runtimeDir?: string | null
  ffmpegDir?: string | null
}

export function resolveDevEngineLocation(resourcesRoot: string, env: NodeJS.ProcessEnv = process.env, home: string = homedir(),
  read: (file: string) => string = file => readFileSync(file, 'utf8')): EngineLocation {
  let config: { engineRoot?: unknown; python?: unknown; runtimeDir?: unknown; ffmpegDir?: unknown } = {}
  try { config = JSON.parse(read(join(resourcesRoot, '.cache', 'a2v-engine.local.json'))) as typeof config } catch { /* 没有就用默认 */ }
  const pick = (...values: unknown[]): string | null => {
    for (const value of values) if (typeof value === 'string' && value.trim() && isAbsolute(value.trim())) return value.trim()
    return null
  }
  return {
    root: pick(env.A2V_ENGINE_ROOT, config.engineRoot, join(home, 'dev', 'a2v-engine-p32')),
    python: pick(env.A2V_ENGINE_PYTHON, config.python, process.platform === 'win32' ? null : join(home, 'dev', 'a2v-venv', 'bin', 'python')),
    runtimeDir: pick(env.A2V_RUNTIME_DIR, config.runtimeDir, process.platform === 'win32' ? null : join(home, 'dev', 'a2v-runtime')),
    ffmpegDir: pick(env.A2V_FFMPEG_DIR, config.ffmpegDir, process.platform === 'win32' ? null : join(home, 'dev', 'a2v-runtime', 'bin'))
  }
}

// 引擎版本：VERSION 文件 → git 标签 → 未知（null）。只读，不改引擎目录。
// APP3：安装版的 VERSION 由打包脚本写进 Runtime Pack 的引擎目录，内容是"标签 提交号"，如 `v5.7 543a49b`，
// 显示为 `v5.7（543a49b）`；只有版本号（如 `5.7`）也认。开发用的 worktree 没有 VERSION，读 git 标签。
export function readEngineVersion(root: string | null,
  readFile: (file: string) => string = file => readFileSync(file, 'utf8'),
  gitTag: (cwd: string) => string | null = defaultGitTag): string | null {
  if (!root) return null
  try {
    const match = /^(v?\d+(?:\.\d+)*)(?:\s+([0-9a-f]{7,40}))?$/.exec(readFile(join(root, 'VERSION')).trim())
    if (match) {
      const tag = match[1]!.startsWith('v') ? match[1]! : `v${match[1]!}`
      return match[2] ? `${tag}（${match[2].slice(0, 7)}）` : tag
    }
  } catch { /* 没有 VERSION 文件 */ }
  const tag = gitTag(root)
  if (tag && /^v\d+(\.\d+)*$/.test(tag)) return tag
  // APP2：开发用的 p31-service 分支不在标签上：git describe 形如 v5.6-1-ga7d23f0
  return tag && /^v\d+(\.\d+)*-\d+-g[0-9a-f]+$/.test(tag) ? tag : null
}

function defaultGitTag(cwd: string): string | null {
  try {
    return execFileSync('git', ['describe', '--tags', 'HEAD'], { cwd, encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'] }).trim() || null
  } catch {
    return null
  }
}

export function describeEngine(location: EngineLocation, version: string | null,
  exists: (path: string) => boolean = existsSync): EngineInfo {
  const problems: string[] = []
  const isDir = (path: string): boolean => { try { return statSync(path).isDirectory() } catch { return false } }
  if (!location.root) problems.push('没有配置引擎目录')
  else if (!exists(join(location.root, 'pipeline', 'engine', '__main__.py')) || !isDir(location.root)) problems.push(`引擎目录不完整：${location.root}`)
  if (!location.python) problems.push('没有配置 Python')
  else if (!exists(location.python)) problems.push(`找不到 Python：${location.python}`)
  return { version, root: location.root, python: location.python, status: problems.length ? 'missing' : 'ready', problems }
}
