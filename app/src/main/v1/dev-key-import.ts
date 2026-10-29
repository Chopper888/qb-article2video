import { readFileSync } from 'node:fs'
import { KEY_ENV_VARS, type KeyFieldId } from '../../shared/v1/keys'
import type { SecretStore } from './secret-store'

// 开发脚本用：把 ~/.a2v_env 这类 "export NAME=value" 文件里的密钥存进 APP 的加密存储。
// 只认六个变量名；不打印、不记录值，返回导入了哪些变量名。安装版不调用。
export function parseEnvFile(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const raw of text.split(/\r?\n/)) {
    const match = /^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$/.exec(raw)
    if (!match) continue
    const name = match[1]!
    let value = match[2] ?? ''
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1)
    if (value) out[name] = value
  }
  return out
}

export function importKeysFromEnvFile(file: string, store: SecretStore, read: (file: string) => string = f => readFileSync(f, 'utf8')): string[] {
  const values = parseEnvFile(read(file))
  const imported: string[] = []
  for (const [id, envName] of Object.entries(KEY_ENV_VARS) as [KeyFieldId, string][]) {
    if (values[envName]) { store.set(id, values[envName]); imported.push(envName) }
  }
  return imported
}
