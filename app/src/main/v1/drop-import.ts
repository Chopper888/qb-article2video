import { copyFileSync, mkdirSync, statSync } from 'node:fs'
import { basename, extname, isAbsolute, join } from 'node:path'

// APP4：一次拖进窗口多个文件（一个 md 加若干图片）时，APP 在工作文件夹的 _a2v_import/ 下建一个新文件夹，
// 把这些文件复制进去（图片和 md 放在同一层），再按文件夹导入。原文件不动。
// 只拖进一个东西（文件夹、zip、单个 md）时不经过这里，直接交给引擎。

export const IMPORT_DIRNAME = '_a2v_import'
export const MAX_DROPPED = 500

export interface DropFs {
  stat(path: string): { isFile(): boolean; isDirectory(): boolean }
  mkdir(path: string): void
  copy(from: string, to: string): void
}

const realFs: DropFs = {
  stat: path => statSync(path),
  mkdir: path => mkdirSync(path, { recursive: true }),
  copy: (from, to) => copyFileSync(from, to)
}

const pad = (n: number): string => String(n).padStart(2, '0')
export function dropFolderName(now: Date = new Date(), random: () => number = Math.random): string {
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  return `drop-${stamp}-${Math.floor(random() * 0x10000).toString(16).padStart(4, '0')}`
}

// 检查拖进来的多个文件；有问题抛出一句给用户看的话（不建文件夹、不复制）
export function checkDroppedFiles(paths: string[], fs: Pick<DropFs, 'stat'> = realFs): string[] {
  if (paths.length > MAX_DROPPED) throw new Error(`一次拖进来的文件太多（${paths.length} 个），请把文章和图片放进一个文件夹后拖文件夹`)
  for (const path of paths) {
    if (typeof path !== 'string' || !isAbsolute(path)) throw new Error('拖进来的文件路径不对')
    let info: ReturnType<DropFs['stat']>
    try { info = fs.stat(path) } catch { throw new Error(`找不到拖进来的文件：${basename(path)}`) }
    if (info.isDirectory()) throw new Error('一次拖进多个时只能是文件（一个 md 加若干图片）；文件夹请单独拖进来')
    if (!info.isFile()) throw new Error(`不是普通文件：${basename(path)}`)
    if (extname(path).toLowerCase() === '.zip') throw new Error('压缩包请单独拖进来')
  }
  const mds = paths.filter(path => extname(path).toLowerCase() === '.md')
  if (!mds.length) throw new Error('拖进来的文件里没有 md 文章，没有导入')
  if (mds.length > 1) throw new Error(`拖进来的文件里有 ${mds.length} 个 md，一次只能导入一篇，没有导入`)
  const seen = new Set<string>()
  for (const path of paths) {
    const name = basename(path).normalize('NFC').toLowerCase()
    if (seen.has(name)) throw new Error(`拖进来的文件里有同名文件：${basename(path)}，没有导入`)
    seen.add(name)
  }
  return paths
}

// 复制进 <工作文件夹>/_a2v_import/drop-…/，返回这个文件夹（交给引擎按文件夹导入）
export function stageDroppedFiles(paths: string[], workRoot: string, fs: DropFs = realFs, name: string = dropFolderName()): string {
  checkDroppedFiles(paths, fs)
  if (!isAbsolute(workRoot)) throw new Error('工作文件夹必须是绝对路径')
  const dir = join(workRoot, IMPORT_DIRNAME, name)
  fs.mkdir(dir)
  for (const path of paths) fs.copy(path, join(dir, basename(path)))
  return dir
}

// 1.0.1（R101 M）："改好了，重新检查"要看用户改过的原始文件，不是上次复制出来的副本：
// 重新读一遍原始文件，复制进同一个 drop-… 文件夹（同一路径，引擎复用同一篇文章，按新内容同步）。
// 原始文件被移动或删掉了：给一句大白话，不导入。
export function restageDroppedFiles(paths: string[], dir: string, fs: DropFs = realFs): string {
  const gone = paths.filter(path => { try { return !fs.stat(path).isFile() } catch { return true } })
  if (gone.length) {
    throw new Error(`原来拖进来的文件找不到了（可能被移动、改名或删除了）：${gone.map(path => basename(path)).join('、')}。请把文章和图片重新拖进来`)
  }
  checkDroppedFiles(paths, fs)
  fs.mkdir(dir)
  for (const path of paths) fs.copy(path, join(dir, basename(path)))
  return dir
}
