import { win32 } from 'node:path'

// Only resolves paths. Call inside the existing process-operation error boundary.
export function resolveWindowsSystemPaths(environment: NodeJS.ProcessEnv): {
  systemRoot: string
  powershell: string
  taskkill: string
} {
  const value = (name: string): string | undefined => Object.keys(environment)
    .filter(key => key.toUpperCase() === name)
    .map(key => environment[key]?.trim())
    .find(candidate => Boolean(candidate))
  const systemRoot = value('SYSTEMROOT') ?? value('WINDIR')
  if (!systemRoot) throw new Error('无法解析 Windows 系统根：SystemRoot/WINDIR 缺失或为空')
  // A drive-relative or current-drive-rooted value would reintroduce cwd dependence.
  if (!/^(?:[a-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+(?:[\\/]|$))/i.test(systemRoot) || /[\0\r\n]/.test(systemRoot)) {
    throw new Error('无法解析 Windows 系统根：SystemRoot/WINDIR 必须为完整绝对路径')
  }
  return {
    systemRoot: win32.normalize(systemRoot),
    powershell: win32.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    taskkill: win32.join(systemRoot, 'System32', 'taskkill.exe')
  }
}
