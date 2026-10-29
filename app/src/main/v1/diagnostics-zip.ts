import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

// 导出诊断信息（任务书 APP2 3.4.3）：生成一个 zip，内容是 APP 版本、引擎版本、系统信息、APP 日志、
// 最近一次任务的引擎事件流和检查结果.json。导出前用已存的密钥对全部内容做一次全文扫描，命中就拒绝导出。
// zip 用"只存储不压缩"格式自己写（不加依赖）。

export interface ZipEntry { name: string; data: Buffer }

const CRC_TABLE = (() => {
  const table = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  return table
})()

export function crc32(data: Buffer): number {
  let c = 0xffffffff
  for (const byte of data) c = CRC_TABLE[(c ^ byte) & 0xff]! ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

export function buildZip(entries: ZipEntry[], date: Date = new Date()): Buffer {
  const dosTime = ((date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2)) & 0xffff
  const dosDate = (((date.getFullYear() - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate()) & 0xffff
  const locals: Buffer[] = []
  const centrals: Buffer[] = []
  let offset = 0
  for (const entry of entries) {
    const name = Buffer.from(entry.name, 'utf8')
    const crc = crc32(entry.data)
    const local = Buffer.alloc(30)
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(0x0800, 6); local.writeUInt16LE(0, 8)
    local.writeUInt16LE(dosTime, 10); local.writeUInt16LE(dosDate, 12); local.writeUInt32LE(crc, 14)
    local.writeUInt32LE(entry.data.length, 18); local.writeUInt32LE(entry.data.length, 22); local.writeUInt16LE(name.length, 26); local.writeUInt16LE(0, 28)
    const central = Buffer.alloc(46)
    central.writeUInt32LE(0x02014b50, 0); central.writeUInt16LE(20, 4); central.writeUInt16LE(20, 6); central.writeUInt16LE(0x0800, 8)
    central.writeUInt16LE(0, 10); central.writeUInt16LE(dosTime, 12); central.writeUInt16LE(dosDate, 14); central.writeUInt32LE(crc, 16)
    central.writeUInt32LE(entry.data.length, 20); central.writeUInt32LE(entry.data.length, 24); central.writeUInt16LE(name.length, 28)
    central.writeUInt32LE(offset, 42)
    locals.push(local, name, entry.data)
    centrals.push(central, name)
    offset += local.length + name.length + entry.data.length
  }
  const centralBuf = Buffer.concat(centrals)
  const end = Buffer.alloc(22)
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10)
  end.writeUInt32LE(centralBuf.length, 12); end.writeUInt32LE(offset, 16)
  return Buffer.concat([...locals, centralBuf, end])
}

// 每个密钥查明文、base64、URL 编码三种写法；太短的值（< 6 个字符）不查，免得误伤
export function secretNeedles(secrets: string[]): Buffer[] {
  const out: Buffer[] = []
  for (const secret of secrets) {
    if (!secret || secret.length < 6) continue
    for (const form of new Set([secret, Buffer.from(secret, 'utf8').toString('base64').replace(/=+$/, ''), encodeURIComponent(secret)])) out.push(Buffer.from(form, 'utf8'))
  }
  return out
}

// 返回命中密钥的条目名（不返回密钥本身）
export function findSecretHits(entries: ZipEntry[], secrets: string[]): string[] {
  const needles = secretNeedles(secrets)
  return entries.filter(entry => needles.some(needle => entry.data.includes(needle) || Buffer.from(entry.name, 'utf8').includes(needle))).map(entry => entry.name)
}

export interface DiagnosticsInput {
  info: Record<string, unknown>
  logsDir: string
  lastEvents: unknown[] | null
  lastReportFile: string | null
}

export function collectDiagnostics(input: DiagnosticsInput): ZipEntry[] {
  const entries: ZipEntry[] = [{ name: '信息.json', data: Buffer.from(JSON.stringify(input.info, null, 2), 'utf8') }]
  let logs: string[] = []
  try { logs = readdirSync(input.logsDir).filter(name => { try { return statSync(join(input.logsDir, name)).isFile() } catch { return false } }).sort() } catch { /* 没有日志 */ }
  for (const name of logs) {
    try { entries.push({ name: `APP日志/${name}`, data: readFileSync(join(input.logsDir, name)) }) } catch { /* 读不到跳过 */ }
  }
  if (input.lastEvents?.length) entries.push({ name: '最近一次任务/引擎事件流.jsonl', data: Buffer.from(input.lastEvents.map(event => JSON.stringify(event)).join('\n') + '\n', 'utf8') })
  if (input.lastReportFile) {
    try { entries.push({ name: '最近一次任务/检查结果.json', data: readFileSync(input.lastReportFile) }) } catch { /* 没有检查结果 */ }
  }
  return entries
}

export class SecretLeakError extends Error {
  constructor(readonly entries: string[]) { super(`诊断信息里发现了密钥（${entries.join('、')}），已拒绝导出`) }
}

// 扫描通过才写文件；命中就抛 SecretLeakError，不写任何文件
export function writeDiagnosticsZip(target: string, entries: ZipEntry[], secrets: string[], write: (file: string, data: Buffer) => void = writeFileSync): { bytes: number; entries: string[] } {
  const hits = findSecretHits(entries, secrets)
  if (hits.length) throw new SecretLeakError(hits)
  const zip = buildZip(entries)
  write(target, zip)
  return { bytes: zip.length, entries: entries.map(entry => entry.name) }
}
