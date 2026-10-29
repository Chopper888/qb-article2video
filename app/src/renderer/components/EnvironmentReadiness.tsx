import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { EnvironmentReadiness } from '../../shared/types'

type ReadinessState = {
  result: EnvironmentReadiness | null; loading: boolean; refresh: () => Promise<void>
}
const ReadinessContext = createContext<ReadinessState | null>(null)

export function EnvironmentReadinessProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const readiness = useReadinessQuery(true)
  return <ReadinessContext.Provider value={readiness}>{children}</ReadinessContext.Provider>
}

export function useEnvironmentReadiness(enabled = true): ReadinessState {
  const shared = useContext(ReadinessContext)
  const local = useReadinessQuery(enabled && !shared)
  return shared ?? local
}

function useReadinessQuery(enabled: boolean): ReadinessState {
  const [result, setResult] = useState<EnvironmentReadiness | null>(null)
  const [loading, setLoading] = useState(true)
  const request = useRef(0)
  const refresh = useCallback(async () => {
    const current = ++request.current
    setLoading(true)
    try {
      const snapshot = await window.appApi.getEnvironmentReadiness()
      if (current === request.current) setResult(snapshot)
    } catch {
      if (current === request.current) setResult({ status: 'blocked', checks: [
        { id: 'readiness_unavailable', status: 'blocked', message: '无法读取运行环境，请重新检查' }
      ] })
    } finally {
      if (current === request.current) setLoading(false)
    }
  }, [])
  useEffect(() => {
    if (enabled) void refresh()
    return () => { request.current++ }
  }, [enabled, refresh])
  return { result, loading, refresh }
}

export function EnvironmentReadinessPanel({ result, loading, refresh }: ReturnType<typeof useEnvironmentReadiness>): React.JSX.Element {
  return <div className="section-card" aria-live="polite">
    <div className="row-between"><h2>运行环境：{loading ? '检查中' : result?.status === 'ready' ? '正常' : '未就绪'}</h2>
      <button className="secondary-button" disabled={loading} onClick={() => void refresh()}>重新检查环境</button></div>
    {!loading && result?.status === 'ready' && <p>当前已知路径检查通过；启动时将再次检查。</p>}
    {!loading && result?.checks.filter(check => check.status === 'blocked').map(check => <p key={check.id}>{check.message}</p>)}
    {!loading && result && <details><summary>检查详情</summary>{result.checks.map(check => <div key={check.id}><p>{check.message}</p>{check.path && <p>{check.path}</p>}</div>)}</details>}
  </div>
}
