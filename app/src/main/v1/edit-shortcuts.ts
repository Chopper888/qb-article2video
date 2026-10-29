import type { Event as ElectronEvent, Input, WebContents } from 'electron'

// 1.0.1（R101 H）：输入框里的撤销 / 重做。
// Mac 有"编辑"菜单（app-menu.ts），⌘Z / ⌘⇧Z 由菜单的 undo / redo 角色处理。
// Windows / Linux 不设菜单（APP4：不要顶部菜单栏），没有菜单的 undo / redo 角色；1.0.0 在 Windows 上实测 Ctrl+Z 没反应。
// 这里不依赖菜单，在主进程按键入口把 Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z 接下来，交给 webContents.undo() / redo()；
// 原按键事件拦掉（preventDefault），不会撤销两次。只认这三个组合，其他按键原样交给页面。
export type EditAction = 'undo' | 'redo'

type KeyInput = Pick<Input, 'type' | 'key' | 'control' | 'meta' | 'shift' | 'alt'>

export function editActionFor(input: KeyInput, platform: NodeJS.Platform): EditAction | null {
  if (platform === 'darwin') return null
  if (input.type !== 'keyDown' || !input.control || input.alt || input.meta) return null
  const key = input.key.toLowerCase()
  if (key === 'z') return input.shift ? 'redo' : 'undo'
  if (key === 'y' && !input.shift) return 'redo'
  return null
}

export function installEditShortcuts(contents: Pick<WebContents, 'on' | 'undo' | 'redo'>, platform: NodeJS.Platform = process.platform): void {
  if (platform === 'darwin') return
  contents.on('before-input-event', (event: ElectronEvent, input: Input) => {
    const action = editActionFor(input, platform)
    if (!action) return
    event.preventDefault()
    if (action === 'undo') contents.undo(); else contents.redo()
  })
}
