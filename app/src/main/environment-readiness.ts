import { constants } from 'node:fs'
import { access, readFile, realpath, stat } from 'node:fs/promises'
import { dirname, isAbsolute, relative, sep, win32 } from 'node:path'
import type { SampleResources } from '../engine/engine-resources'
import { resolveWindowsSystemPaths } from '../engine/windows-system-paths'
import type { EnvironmentCheck, EnvironmentReadiness } from '../shared/types'
import type { AppPaths } from './app-paths'
import { manifestObject, resolvePackagedResources, validatePackagedManifest } from './packaged-resources'
import { RuntimeWorkspace, type RuntimePreflight } from './runtime-workspace'
import sampleContract from '../shared/v5-sample-contract.json'

interface ReadinessFileSystem {
  stat(path: string): Promise<{ isDirectory(): boolean; isFile(): boolean }>
  access(path: string, mode: number): Promise<void>
  realpath(path: string): Promise<string>
  readFile?(path: string): Promise<string>
}
type ReadinessCheck = (id: string, label: string, inspect: () => Promise<void>, path?: string) => Promise<void>

function within(root: string, path: string): boolean {
  const child = relative(root, path)
  return child === '' || (!isAbsolute(child) && child !== '..' && !child.startsWith(`..${sep}`))
}

// No process execution, directory scanning, probes, creation or cached verdicts.
export class EnvironmentReadinessService {
  constructor(private readonly paths: AppPaths, private readonly resources: SampleResources | null,
    private readonly environment: NodeJS.ProcessEnv = process.env,
    private readonly fs: ReadinessFileSystem = { stat, access, realpath, readFile: path => readFile(path, 'utf8') },
    private readonly runtimeWorkspace: RuntimePreflight = new RuntimeWorkspace(paths.isPackaged ? resolvePackagedResources(paths) : resources, environment)) {}

  async check(): Promise<EnvironmentReadiness> {
    const checks: EnvironmentCheck[] = []
    const check = async (id: string, label: string, inspect: () => Promise<void>, path?: string): Promise<void> => {
      try {
        await inspect()
        checks.push({ id, status: 'ready', message: `${label}正常`, ...(path ? { path } : {}) })
      } catch (error) {
        const detail = id === 'runtime_workspace' && error instanceof Error ? `：${error.message}` : ''
        checks.push({ id, status: 'blocked', message: `${label}未就绪${detail}`, ...(path ? { path } : {}) })
      }
    }
    await check('user_data', '用户数据目录（须存在且可写）', async () => {
      await this.directory(this.paths.userDataRoot)
      await this.fs.access(this.paths.userDataRoot, constants.W_OK)
    }, this.paths.userDataRoot)
    await check('resources_root', '应用资源目录（须为已存在的绝对目录）', () => this.directory(this.paths.resourcesRoot), this.paths.resourcesRoot)
    await check('real_task_root', '真实任务目录（须在用户数据内、应用资源外且可写）', () => this.taskRoot(), this.paths.realTaskRoot)
    await check('windows_system_tools', 'Windows 系统根与系统工具路径（须已配置且文件存在）', async () => {
      const paths = resolveWindowsSystemPaths(this.environment)
      await this.file(paths.powershell)
      await this.file(paths.taskkill)
    })
    if (this.paths.isPackaged) {
      await this.packagedChecks(check)
    } else {
      const resources = this.resources
      for (const [id, label, path, file] of [
        ['development_engine', '开发引擎目录', resources?.engineRoot, false],
        ['development_python', '开发 Python 文件', resources?.pythonExecutable, true],
        ['development_runtime', '开发运行时目录', resources?.runtimeSource, false],
        ['development_sample', '开发样本目录', resources?.sampleSourceRoot, false]
      ] as const) {
        await check(id, label, async () => {
          if (!path) throw new Error('未配置')
          await (file ? this.file(path) : this.directory(path))
        }, path)
      }
    }
    await check('runtime_workspace', '短 Runtime 工作目录', async () => { await this.runtimeWorkspace.check() })
    return { status: checks.some(check => check.status === 'blocked') ? 'blocked' : 'ready', checks }
  }

  private async json(path: string): Promise<Record<string, unknown>> {
    await this.file(path)
    if (!this.fs.readFile) throw new Error('文件读取不可用')
    return manifestObject(JSON.parse(await this.fs.readFile(path)))
  }

  private async packagedChecks(check: ReadinessCheck): Promise<void> {
    const resources = resolvePackagedResources(this.paths)
    let manifest: Record<string, unknown> | null = null
    await check('packaged_runtime_manifest', '视频引擎资源清单（v5 / R7c 工具链）', async () => {
      if (!resources) throw new Error('发布资源路径不可用')
      manifest = validatePackagedManifest(await this.json(resources.manifestPath))
    }, resources?.manifestPath)
    // A malformed resource root is reported, never replaced by development configuration.
    if (!resources) return
    const files = async (root: string, names: string[]): Promise<void> => {
      for (const name of names) await this.file(win32.join(root, name))
    }
    await check('packaged_engine', '视频引擎与样本准备工具', async () => {
      await this.directory(win32.join(resources.engineRoot, 'pipeline', 'engine'))
      await this.directory(win32.join(resources.engineRoot, 'R57-render-test', 'src'))
      await files(resources.engineRoot, ['pipeline/engine/__init__.py', 'pipeline/scene_build.py',
        'R148/r148_subtitle_align.py', 'R57-render-test/package.json', 'R57-render-test/package-lock.json',
        'R57-render-test/tsconfig.json', 'tools/prepare_sample.py'])
      if (!manifest) throw new Error('白名单不可用')
      const entries = manifestObject(manifestObject(manifest.enginePublishWhitelist).filesSha256)
      const articles = Object.keys(entries).filter(name => /^R168\/[^/\\]+\.md$/.test(name))
      if (articles.length !== 1) throw new Error('样本文章不可用')
      await files(resources.engineRoot, articles)
    }, resources.engineRoot)
    await check('packaged_python', '私有 Python 与 VC 运行库', () => files(resources.pythonRoot,
      ['python.exe', 'python312.dll', 'python312.zip', 'python312._pth', 'vcruntime140.dll', 'vcruntime140_1.dll']), resources.pythonRoot)
    await check('packaged_node', '私有 Node / npm / npx', () => files(resources.nodeRoot,
      ['node.exe', 'npm.cmd', 'npx.cmd', 'node_modules/npm/bin/npm-cli.js', 'node_modules/npm/bin/npx-cli.js']), resources.nodeRoot)
    await check('packaged_ffmpeg', '私有 FFmpeg / ffprobe 与共享运行库', async () => {
      await this.directory(resources.ffmpegBin)
      await files(resources.ffmpegBin, ['ffmpeg.exe', 'ffprobe.exe', 'avcodec-63.dll', 'avdevice-63.dll',
        'avfilter-12.dll', 'avformat-63.dll', 'avutil-61.dll', 'swresample-7.dll', 'swscale-10.dll'])
    }, resources.ffmpegBin)
    await check('packaged_runtime', '视频运行时、浏览器与字体', async () => {
      await this.directory(win32.join(resources.runtimeSource, 'node_modules'))
      const runtime = await this.json(win32.join(resources.runtimeSource, 'manifest.json'))
      if (runtime.schema_version !== 1) throw new Error('Runtime manifest 不符')
      const versions = manifestObject(runtime.remotion_packages)
      for (const name of ['remotion', '@remotion/cli', '@remotion/renderer', '@remotion/transitions', '@remotion/google-fonts']) {
        const folder = win32.join(resources.runtimeSource, 'node_modules', name)
        await this.directory(folder)
        if (versions[name] !== '4.0.505' || (await this.json(win32.join(folder, 'package.json'))).version !== '4.0.505') throw new Error('Remotion 版本不符')
      }
      await files(resources.runtimeSource, [
        'node_modules/.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64/chrome-headless-shell.exe',
        'node_modules/@remotion/compositor-win32-x64-msvc/remotion.exe',
        'node_modules/@remotion/compositor-win32-x64-msvc/ffmpeg.exe',
        'node_modules/@remotion/compositor-win32-x64-msvc/ffprobe.exe',
        'node_modules/@esbuild/win32-x64/esbuild.exe', 'fonts/LXGWWenKai-Regular.ttf'])
      await files(resources.runtimePackRoot, ['licenses/provenance.json', 'licenses/fonts/LXGW-WenKai/OFL.txt'])
    }, resources.runtimeSource)
    await check('packaged_sample', '示例篇样本源', async () => {
      await this.directory(resources.sampleSourceRoot)
      const sample = await this.json(win32.join(resources.sampleSourceRoot, 'manifest.json'))
      if (sample.schema_version !== 1 || sample.total_files !== sampleContract.totalFiles) throw new Error('样本清单不符')
      const entries = manifestObject(sample.files)
      const required = ['narration.txt', 'narration_meta.json', 'narration_tts.txt', 'sentences.json',
        'query_response.json', 'audio/r168_doubao_full.wav', 'manifest.json'].map(name => `R168/${name}`)
      for (const name of required) if (!entries[name]) throw new Error('样本清单缺关键文件')
      for (const [name, sha256] of Object.entries(sampleContract.s7Cache)) {
        if (manifestObject(entries[name]).sha256 !== sha256) throw new Error('S7 样本缓存清单不符')
      }
      await files(resources.sampleSourceRoot, required)
      await files(resources.sampleSourceRoot, Object.keys(sampleContract.s7Cache))
    }, resources.sampleSourceRoot)
  }

  private async directory(path: string): Promise<void> {
    if (!isAbsolute(path) || !(await this.fs.stat(path)).isDirectory()) throw new Error('目录不可用')
  }

  private async file(path: string): Promise<void> {
    if (!isAbsolute(path) || !(await this.fs.stat(path)).isFile()) throw new Error('文件不可用')
  }

  private async taskRoot(): Promise<void> {
    const { userDataRoot, realTaskRoot, resourcesRoot } = this.paths
    // Only the explicit development smoke fact from app-paths waives resource isolation.
    const allowResourceOverlap = !this.paths.isPackaged && this.paths.usesLocalUserData === true
    if (!isAbsolute(realTaskRoot) || !within(userDataRoot, realTaskRoot) ||
      relative(userDataRoot, realTaskRoot) === '' ||
      (!allowResourceOverlap && within(resourcesRoot, realTaskRoot))) throw new Error('目录边界不符')
    await this.directory(userDataRoot)
    const userRoot = await this.fs.realpath(userDataRoot)
    const resourceRoot = await this.fs.realpath(resourcesRoot)
    let existing = realTaskRoot
    // An absent task root is usable if its nearest existing parent is writable.
    // Resolve existing ancestors to reject junctions escaping the private root.
    for (;;) {
      try {
        await this.directory(existing)
        break
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT' || existing === userDataRoot) throw error
        existing = dirname(existing)
      }
    }
    const actual = await this.fs.realpath(existing)
    if (!within(userRoot, actual) || (!allowResourceOverlap && within(resourceRoot, actual)) ||
      (existing === realTaskRoot && relative(userRoot, actual) === '')) throw new Error('实际目录边界不符')
    await this.fs.access(existing, constants.W_OK)
  }
}
