import { spawn, type ChildProcessWithoutNullStreams, type SpawnOptionsWithoutStdio } from 'node:child_process'
import { StringDecoder } from 'node:string_decoder'
import type { ProcessExitFact } from '../shared/types'
import type { ProcessCommand } from './real-engine-command'

export interface ProcessObserver {
  stdout(chunk: Uint8Array): void
  stderr(text: string): void
  spawned(pid: number | null): void
}

export interface ProcessTransport {
  execute(command: ProcessCommand, observer: ProcessObserver): Promise<ProcessExitFact>
}

export type SpawnProcess = (executable: string, args: string[], options: SpawnOptionsWithoutStdio) => ChildProcessWithoutNullStreams

// Only process and byte-stream facts live here. No event parsing, success rules or Stop API.
export class NodeProcessTransport implements ProcessTransport {
  constructor(private readonly spawnProcess: SpawnProcess = spawn) {}

  execute(command: ProcessCommand, observer: ProcessObserver): Promise<ProcessExitFact> {
    return new Promise((resolve) => {
      let child: ChildProcessWithoutNullStreams
      try {
        child = this.spawnProcess(command.executable, command.args, {
          cwd: command.cwd, env: command.env, shell: false, windowsHide: true, stdio: 'pipe',
          ...(command.detached ? { detached: true } : {})
        })
      } catch (error) {
        resolve({ code: null, signal: null, error: error instanceof Error ? error.message : String(error) })
        return
      }
      const stderr = new StringDecoder('utf8')
      let spawnError: string | null = null
      child.once('spawn', () => observer.spawned(child.pid ?? null))
      child.stdout.on('data', (chunk: Buffer) => observer.stdout(chunk))
      child.stderr.on('data', (chunk: Buffer) => {
        const text = stderr.write(chunk)
        if (text) observer.stderr(text)
      })
      child.on('error', (error: Error) => { spawnError = error.message })
      // close follows stdio closure (including spawn failure); exit alone can lose trailing JSONL.
      child.once('close', (code: number | null, signal: string | null) => {
        const tail = stderr.end()
        if (tail) observer.stderr(tail)
        resolve({ code, signal, error: spawnError })
      })
      child.stdin.end()
    })
  }
}
