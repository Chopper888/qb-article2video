import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

// 成片库的封面和时长（任务书 APP2 3.3）：封面用 ffmpeg 从第 10 秒截一帧（不足 10 秒截第 1 秒），
// 缓存在 APP 用户数据目录；时长用 ffprobe 读。ffmpeg、ffprobe 都在 ffmpegDir 里（打包版随包，开发版可配置）。

export type ExecFn = (file: string, args: string[], options: { timeout: number; windowsHide: true }) => Promise<string>

const defaultExec: ExecFn = (file, args, options) => new Promise((resolve, reject) => {
  execFile(file, args, { ...options, encoding: 'utf8', maxBuffer: 1024 * 1024 }, (error, stdout) => error ? reject(error) : resolve(stdout))
})

export function toolPath(ffmpegDir: string | null, name: 'ffmpeg' | 'ffprobe', platform: NodeJS.Platform = process.platform): string {
  const exe = platform === 'win32' ? `${name}.exe` : name
  return ffmpegDir ? join(ffmpegDir, exe) : exe
}

export async function probeDuration(file: string, ffmpegDir: string | null, exec: ExecFn = defaultExec): Promise<number | null> {
  try {
    const out = await exec(toolPath(ffmpegDir, 'ffprobe'), ['-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file], { timeout: 30_000, windowsHide: true })
    const value = Number.parseFloat(out.trim())
    return Number.isFinite(value) && value > 0 ? value : null
  } catch { return null }
}

export function coverCacheFile(cacheDir: string, file: string): string {
  let stamp = ''
  try { const st = statSync(file); stamp = `${st.size}:${st.mtimeMs}` } catch { /* 读不到就只按路径 */ }
  return join(cacheDir, `${createHash('sha256').update(`${file}\0${stamp}`).digest('hex').slice(0, 32)}.jpg`)
}

// 返回封面 data URL；截不出来返回 null
export async function coverDataUrl(file: string, durationSec: number | null, ffmpegDir: string | null, cacheDir: string, exec: ExecFn = defaultExec): Promise<string | null> {
  const cached = coverCacheFile(cacheDir, file)
  if (!existsSync(cached)) {
    const at = durationSec !== null && durationSec < 10 ? 1 : 10
    try {
      mkdirSync(cacheDir, { recursive: true })
      await exec(toolPath(ffmpegDir, 'ffmpeg'), ['-v', 'error', '-y', '-ss', String(at), '-i', file, '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '4', cached], { timeout: 60_000, windowsHide: true })
    } catch { return null }
  }
  try { return `data:image/jpeg;base64,${readFileSync(cached).toString('base64')}` } catch { return null }
}
