// Transport-free parser: accepts text or byte chunks, never reads files/processes.
export interface WireEvent extends Record<string, unknown> {
  schema_version: 1
  seq: number
  time: string
  event: string
  step: string | null
  message: string
  name?: string
  outputs?: string[]
  exit_code?: number
  reason?: string
  delivery_path?: string | null
}

export type JsonlResult =
  | { kind: 'event' | 'unknown'; event: WireEvent; raw: string }
  | { kind: 'diagnostic'; message: string; raw: string }

const events = new Set(['step_start', 'step_done', 'step_skip', 'step_fail', 'run_done'])
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)

function parseLine(raw: string): JsonlResult {
  try {
    const value: unknown = JSON.parse(raw)
    if (!object(value) || value.schema_version !== 1 || !Number.isInteger(value.seq) || Number(value.seq) < 1 ||
      typeof value.time !== 'string' || !Number.isFinite(Date.parse(value.time)) || typeof value.event !== 'string' ||
      !(value.step === null || typeof value.step === 'string') || typeof value.message !== 'string') {
      return { kind: 'diagnostic', message: '事件基础字段无效', raw }
    }
    if (!events.has(value.event)) return { kind: 'unknown', event: value as WireEvent, raw }
    if ((value.event !== 'run_done' && (typeof value.step !== 'string' || !value.step)) ||
      (value.event === 'run_done' && !Number.isInteger(value.exit_code)) ||
      ('name' in value && typeof value.name !== 'string') ||
      ('outputs' in value && (!Array.isArray(value.outputs) || !value.outputs.every((item) => typeof item === 'string'))) ||
      ('reason' in value && typeof value.reason !== 'string') ||
      ('delivery_path' in value && value.delivery_path !== null && typeof value.delivery_path !== 'string')) {
      return { kind: 'diagnostic', message: '事件已知字段无效', raw }
    }
    return { kind: 'event', event: value as WireEvent, raw }
  } catch (error) {
    return { kind: 'diagnostic', message: `JSONL 解析失败：${error instanceof Error ? error.message : String(error)}`, raw }
  }
}

export class JsonlParser {
  private readonly decoder = new TextDecoder('utf-8')
  private buffer = ''
  private ended = false

  push(chunk: string | Uint8Array): JsonlResult[] {
    if (this.ended) throw new Error('JSONL 流已经结束')
    this.buffer += typeof chunk === 'string' ? this.decoder.decode() + chunk : this.decoder.decode(chunk, { stream: true })
    return this.drain(false)
  }

  end(): JsonlResult[] {
    if (this.ended) return []
    this.ended = true
    this.buffer += this.decoder.decode()
    return this.drain(true)
  }

  private drain(final: boolean): JsonlResult[] {
    const lines = this.buffer.split('\n')
    this.buffer = lines.pop() ?? ''
    if (final) { lines.push(this.buffer); this.buffer = '' }
    return lines.filter((line) => line.trim()).map((line) => parseLine(line.replace(/\r$/, '')))
  }
}
