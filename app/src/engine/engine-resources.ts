// Injected by Main. These are existing CLI inputs, not a packaged Runtime layout.
export interface EngineResources {
  engineRoot: string
  engineCommit: string
  pythonExecutable: string
  // Main-only injection seam; credentials are created per spawn, never persisted.
  childEnvironment?: (parent: NodeJS.ProcessEnv) => NodeJS.ProcessEnv
}

export interface SampleResources extends EngineResources {
  runtimeSource: string
  sampleSourceRoot: string
}
