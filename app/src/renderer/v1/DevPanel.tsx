import { useEffect, useState } from 'react'
import type { DemoState } from '../../shared/v1/types'

// 开发者开关：Ctrl/⌘ + Shift + D 打开。只在开发模式（或显式 A2V_DEV_TOOLS=1）存在，普通用户看不到。
// 原型里写着"演示"字样的按钮都放在这里，不出现在普通界面上。

const SCENES: [string, string][] = [
  ['onboard:0', '引导·欢迎'], ['onboard:1', '引导·填写密钥'], ['onboard:2', '引导·工作文件夹'],
  ['home-full', '启动页·密钥齐全'], ['home-missing', '启动页·缺一项'],
  ['import:import_problems', '导入有问题'], ['import:import_pass', '检查通过·确认费用'], ['import:import_over_limit', '可能超上限'],
  ['run:run_theory', '出片中·理论型'], ['run:run_screenshot', '出片中·截图型（渲染）'], ['run:network_retry', '网络不稳定'],
  ['run:balance_insufficient', '余额不足'], ['run:key_invalid', '密钥错误'], ['run:cost_limit', '到费用上限'], ['run:missing_images', '缺图'],
  ['import-missing', '确认费用·缺必需密钥'], ['run:stopped', '被停止'], ['run:done_pass', '（演示）跳到完成·通过'], ['run:done_red', '（演示）跳到完成·有标红'],
  ['tasks', '任务列表'], ['library', '成片库'], ['settings:keys', '设置·密钥'], ['settings:general', '设置·通用'], ['settings:folders', '设置·工作文件夹'], ['settings:about', '设置·关于']
]

// onState：开关变了告诉 AppV1（APP4："不付费试跑"打开时顶部常驻提示条）
export function DevPanel({ onScene, onClose, onState }: { onScene(scene: string): void; onClose(): void; onState?(state: DemoState | null): void }): React.JSX.Element {
  const [state, setLocal] = useState<DemoState | null>(null)
  const setState = (next: DemoState | null): void => { setLocal(next); onState?.(next) }
  useEffect(() => { void window.a2v.getDemoState().then(setLocal) }, [])
  const setDelay = (delayMs: number): void => { void window.a2v.setDemoState({ delayMs }).then(setState) }
  const set = (patch: Partial<DemoState>): void => { void window.a2v.setDemoState(patch).then(setState) }
  return (
    <aside className="devpanel" aria-label="开发者开关">
      <div style={{ display: 'flex', justifyContent: 'space-between' }}><h4>开发者开关（演示场景）</h4><button className="link" onClick={onClose}>关闭</button></div>
      <div className="muted">导入 / 出片：{state?.mode === 'demo' ? '演示场景' : '真实引擎'}{state?.noPaid ? ' · 不付费试跑' : ''} · 演示事件间隔 {state?.delayMs} ms</div>
      <div className="grid">
        <button className="chip" aria-pressed={state?.mode !== 'demo'} onClick={() => set({ mode: 'real' })}>真实引擎</button>
        <button className="chip" aria-pressed={state?.mode === 'demo'} onClick={() => set({ mode: 'demo' })}>演示场景</button>
        <button className="chip" aria-pressed={!!state?.noPaid} onClick={() => set({ noPaid: !state?.noPaid })}>不付费试跑（运行、续跑带 --no-paid；不保存，重启就关）</button>
      </div>
      <div className="grid">
        <button className="chip" aria-pressed={state?.delayMs === 0} onClick={() => setDelay(0)}>立即放完</button>
        <button className="chip" aria-pressed={state?.delayMs === 350} onClick={() => setDelay(350)}>按时间放</button>
      </div>
      <div className="grid">{SCENES.map(([id, label]) => <button key={id} className="chip" onClick={() => onScene(id)}>{label}</button>)}</div>
      <div className="grid"><button className="chip" onClick={() => onScene('theme:dark')}>深色</button><button className="chip" onClick={() => onScene('theme:light')}>浅色</button></div>
    </aside>
  )
}
