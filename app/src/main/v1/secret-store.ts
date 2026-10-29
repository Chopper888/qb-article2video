import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { KEY_ENV_VARS, KEY_FIELD_IDS, type KeyFieldId, type KeyStatusMap } from '../../shared/v1/keys'

// 密钥存储：Electron safeStorage 加密后写进 userData。明文只在内存里短暂出现：
// 保存时加密、交给引擎子进程时解密成环境变量；不落盘、不进日志、不回传页面。
export interface Encryptor {
  isEncryptionAvailable(): boolean
  encryptString(plain: string): Buffer
  decryptString(encrypted: Buffer): string
}

interface StoredFile { schema: 1; fields: Partial<Record<KeyFieldId, string>> }

const MAX_LENGTH = 4096

export class SecretStore {
  private fields: Partial<Record<KeyFieldId, string>>

  constructor(private readonly file: string, private readonly encryptor: Encryptor) {
    this.fields = this.load()
  }

  status(): KeyStatusMap {
    const out = {} as KeyStatusMap
    for (const id of KEY_FIELD_IDS) {
      const plain = this.read(id)
      out[id] = { filled: plain !== null, last4: plain && plain.length >= 8 ? plain.slice(-4) : null }
    }
    return out
  }

  // 空串 = 清空这一项
  set(id: KeyFieldId, value: string): KeyStatusMap {
    if (!KEY_FIELD_IDS.includes(id)) throw new Error('未知的密钥项')
    const trimmed = typeof value === 'string' ? value.trim() : ''
    if (!trimmed) {
      delete this.fields[id]
    } else {
      if (trimmed.length > MAX_LENGTH || /[\0\r\n]/.test(trimmed)) throw new Error('密钥格式不对：不能有换行，也不能太长')
      if (!this.encryptor.isEncryptionAvailable()) throw new Error('这台电脑的系统加密不可用，密钥没有保存')
      this.fields[id] = this.encryptor.encryptString(trimmed).toString('base64')
    }
    this.save()
    return this.status()
  }

  // 只给 Main 用：启动引擎子进程时转成环境变量。返回值不得写盘、不得记日志、不得发给页面。
  engineEnv(): Record<string, string> {
    const env: Record<string, string> = {}
    for (const id of KEY_FIELD_IDS) {
      const plain = this.read(id)
      if (plain) env[KEY_ENV_VARS[id]] = plain
    }
    return env
  }

  private read(id: KeyFieldId): string | null {
    const stored = this.fields[id]
    if (!stored) return null
    try {
      return this.encryptor.decryptString(Buffer.from(stored, 'base64')) || null
    } catch {
      // 换了电脑或系统凭据被重置时解不开：当作没填
      return null
    }
  }

  private load(): Partial<Record<KeyFieldId, string>> {
    try {
      const parsed = JSON.parse(readFileSync(this.file, 'utf8')) as StoredFile
      if (parsed?.schema !== 1 || typeof parsed.fields !== 'object' || !parsed.fields) return {}
      const fields: Partial<Record<KeyFieldId, string>> = {}
      for (const id of KEY_FIELD_IDS) if (typeof parsed.fields[id] === 'string') fields[id] = parsed.fields[id]
      return fields
    } catch {
      return {}
    }
  }

  private save(): void {
    mkdirSync(dirname(this.file), { recursive: true })
    const temp = `${this.file}.tmp`
    writeFileSync(temp, JSON.stringify({ schema: 1, fields: this.fields } satisfies StoredFile), { encoding: 'utf8', mode: 0o600 })
    renameSync(temp, this.file)
  }
}
