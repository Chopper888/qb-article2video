import type { EngineEvent, MockTask, RealTask } from '../shared/types'
import type { EngineAdapter } from './engine-adapter'
import { MockEngineAdapter } from './mock-engine-adapter'
import { RealEngineAdapter, type RealProcessOptions } from './real-engine-adapter'
import { NodeProcessTransport } from './process-transport'

export interface EngineAdapters { mock: EngineAdapter<MockTask>; real: EngineAdapter<RealTask> }
export type AdapterFactory = (emit: (event: EngineEvent) => void, tickMs: number) => EngineAdapters

export const createEngineAdapters: AdapterFactory = (emit, tickMs) => ({
  mock: new MockEngineAdapter(emit, tickMs),
  real: new RealEngineAdapter(emit)
})

// Internal controlled entry only; the normal Electron/IPC/UI bootstrap does not use it.
export function createProcessEngineAdapters(options: Omit<RealProcessOptions, 'transport'> & { transport?: RealProcessOptions['transport'] }): AdapterFactory {
  return (emit, tickMs) => ({
    mock: new MockEngineAdapter(emit, tickMs),
    real: new RealEngineAdapter(emit, { ...options, transport: options.transport ?? new NodeProcessTransport() })
  })
}
