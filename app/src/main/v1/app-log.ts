import { appendFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

// APP 自己的运行日志（userData/logs/app-v1.log）。只写状态，不写密钥、不写引擎输出原文。
export function createAppLog(dir: string): { file: string; write(line: string): void } {
  const file = join(dir, 'app-v1.log')
  return {
    file,
    write(line: string): void {
      try {
        mkdirSync(dir, { recursive: true })
        appendFileSync(file, `${new Date().toISOString()} ${line.replace(/[\r\n]+/g, ' ')}\n`, 'utf8')
      } catch { /* 日志写不了不影响使用 */ }
    }
  }
}
