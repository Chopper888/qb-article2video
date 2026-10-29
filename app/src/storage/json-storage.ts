import { copyFile, mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { createDefaultState } from '../shared/defaults'
import type { AppState, StateStorage } from '../shared/types'
import { isAppState } from '../shared/validation'

async function readState(path: string): Promise<AppState | null> {
  try {
    const parsed: unknown = JSON.parse(await readFile(path, 'utf8'))
    return isAppState(parsed) ? parsed : null
  } catch (error) {
    if (error instanceof SyntaxError || (error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw error
  }
}

export class JsonStorage implements StateStorage {
  constructor(private readonly filePath: string) {}

  async load(): Promise<AppState> {
    return (await readState(this.filePath)) ?? (await readState(`${this.filePath}.bak`)) ?? createDefaultState()
  }

  async save(state: AppState): Promise<void> {
    await mkdir(dirname(this.filePath), { recursive: true })
    const temporaryPath = `${this.filePath}.tmp`
    const current = await readState(this.filePath)
    if (current) await copyFile(this.filePath, `${this.filePath}.bak`)
    await writeFile(temporaryPath, `${JSON.stringify(state, null, 2)}\n`, 'utf8')
    await rename(temporaryPath, this.filePath)
  }
}
