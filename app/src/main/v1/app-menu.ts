import type { MenuItemConstructorOptions } from 'electron'

// APP4：窗口顶部不要 File / Edit / View / Window 菜单。
// Windows / Linux：不设任何菜单（输入框里的复制、粘贴、全选、撤销由 Chromium 自己处理）；
// Mac：系统要求保留最小菜单——应用菜单 + 编辑（没有"编辑"菜单，Mac 上 ⌘C / ⌘V / ⌘A / ⌘Z 在输入框里会失效）。
// 原菜单里有用的"重新加载""开发者工具"挪到了 设置 → 关于与诊断。
export function appMenuTemplate(platform: NodeJS.Platform): MenuItemConstructorOptions[] | null {
  if (platform !== 'darwin') return null
  return [
    { role: 'appMenu' },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' },
        { role: 'cut', label: '剪切' }, { role: 'copy', label: '拷贝' }, { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' }
      ]
    }
  ]
}
