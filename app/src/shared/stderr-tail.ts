export const MAX_PERSISTED_STDERR_CHARS = 65_536

export interface BoundedStderr {
  text: string
  truncated: boolean
}

export function appendBoundedDiagnosticText(current: string, chunk: string, alreadyTruncated = false): BoundedStderr {
  if (!chunk) return { text: current, truncated: alreadyTruncated }
  const combined = current + chunk
  if (combined.length <= MAX_PERSISTED_STDERR_CHARS) {
    return { text: combined, truncated: alreadyTruncated }
  }
  return {
    text: combined.slice(-MAX_PERSISTED_STDERR_CHARS),
    truncated: true
  }
}

export function appendBoundedStderr(current: string, chunk: string, alreadyTruncated = false): BoundedStderr {
  return appendBoundedDiagnosticText(current, chunk, alreadyTruncated)
}
