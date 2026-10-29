import type { BrowserWindow } from 'electron'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { IPC_V1 } from '../../shared/v1/ipc'

// 开发用截图：A2V_DEV_CAPTURE=<计划.json>，内容 {"outDir": "...", "shots": [{"scene": "...", "file": "01-xx.png", "waitMs": 1500}]}。
// 依次让页面切到演示场景，等一会儿后截窗口。安装版不调用。
// APP2：真实流程要等引擎跑完，shot 可带 untilText（页面文字出现这个正则才截，最多等 timeoutMs）；file 为空时只切场景不截图。
interface Shot { scene: string; file?: string; waitMs?: number; untilText?: string; timeoutMs?: number }
interface Plan { outDir: string; width?: number; height?: number; shots: Shot[] }

const sleep = (ms: number): Promise<void> => new Promise(resolve => setTimeout(resolve, ms))

export async function runDevCapture(window: BrowserWindow, planFile: string, log: (line: string) => void): Promise<void> {
  const plan = JSON.parse(readFileSync(planFile, 'utf8')) as Plan
  window.setContentSize(plan.width ?? 1440, plan.height ?? 900)
  await sleep(1500)
  for (const shot of plan.shots) {
    if (shot.scene) window.webContents.send(IPC_V1.devScene, shot.scene)
    await sleep(shot.waitMs ?? 1500)
    if (shot.untilText) {
      const deadline = Date.now() + (shot.timeoutMs ?? 120_000)
      const pattern = new RegExp(shot.untilText)
      let text = ''
      while (Date.now() < deadline) {
        text = String(await window.webContents.executeJavaScript('document.body.innerText'))
        if (pattern.test(text)) break
        await sleep(2000)
      }
      log(`dev-capture: ${shot.scene} 等"${shot.untilText}"${pattern.test(text) ? '已出现' : '超时'}`)
      await sleep(800)
    }
    if (!shot.file) continue
    const image = await window.webContents.capturePage()
    const file = join(plan.outDir, shot.file)
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(file, image.toPNG())
    log(`dev-capture: ${shot.file}`)
  }
}
