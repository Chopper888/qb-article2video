// 颜色变量：深色、浅色各一份（数值取自 UI 原型 v1 源文件 D-Hybrid / H-Run）。
// 页面只用 var(--名字)，切换主题时整体换值。

export type ThemeTokens = Record<
  | 'bg' | 'panel' | 'panel2' | 'rail' | 'line' | 'line2' | 'text' | 'muted' | 'faint' | 'accent' | 'onAccent' | 'accentText'
  | 'ok' | 'warn' | 'warnBg' | 'warnLine' | 'danger' | 'dangerBg' | 'dangerLine' | 'input' | 'inputLine' | 'preview'
  | 'previewInner' | 'doneBg' | 'doneFg' | 'todoBg' | 'todoFg' | 'activeBg' | 'activeFg' | 'failBg' | 'failFg' | 'retryBg' | 'retryFg'
  | 'info' | 'iconBg', string>

export const DARK: ThemeTokens = {
  bg: '#1C1C1E', panel: '#1F1F21', panel2: '#2C2C2E', rail: '#161618', line: '#2C2C2E', line2: '#3A3A3C',
  text: '#F2F2F7', muted: '#AEAEB2', faint: '#9C9CA1', accent: '#FF9F0A', onAccent: '#1C1C1E', accentText: '#FF9F0A',
  ok: '#30D158', warn: '#FFB340', warnBg: '#2A2215', warnLine: '#5A4520', danger: '#FF6961', dangerBg: '#2A1A1A', dangerLine: '#5A2A2A',
  input: '#1C1C1E', inputLine: '#48484A', preview: '#000000', previewInner: '#2C2C2E',
  doneBg: '#2E4A36', doneFg: '#D7F5DF', todoBg: '#2C2C2E', todoFg: '#A1A1A6', activeBg: '#FF9F0A', activeFg: '#1C1C1E',
  failBg: '#5A2A2A', failFg: '#F2F2F7', retryBg: '#5A4520', retryFg: '#F2F2F7', info: '#64D2FF', iconBg: '#1C1C1E'
}

export const LIGHT: ThemeTokens = {
  bg: '#FFFFFF', panel: '#F7F7F9', panel2: '#EDEDF0', rail: '#F3F3F5', line: '#E3E3E8', line2: '#D2D2D7',
  // APP2：主色改成和深色一样的橙色。橙底上的字按对比度选深色（#1C1C1E 8.28，白色只有 2.06）；
  // 橙色当文字（链接等）在浅底上不够 4.5，用加深的橙色 accentText。
  text: '#1D1D1F', muted: '#62626A', faint: '#5E5E66', accent: '#FF9F0A', onAccent: '#1C1C1E', accentText: '#9A5800',
  ok: '#1E7A36', warn: '#7A4100', warnBg: '#FFF8EC', warnLine: '#E2BC7E', danger: '#B42340', dangerBg: '#FDEEF0', dangerLine: '#EFB9C2',
  input: '#FFFFFF', inputLine: '#D2D2D7', preview: '#E9E9EE', previewInner: '#E2E2E8',
  doneBg: '#E3F1E7', doneFg: '#155C2B', todoBg: '#EDEDF0', todoFg: '#56565E', activeBg: '#FF9F0A', activeFg: '#1C1C1E',
  failBg: '#F6D5DA', failFg: '#7D1426', retryBg: '#FBE7C6', retryFg: '#5E3200', info: '#0B6487', iconBg: '#FFFFFF'
}

const camelToKebab = (name: string): string => name.replace(/[A-Z]/g, c => `-${c.toLowerCase()}`)

export function applyTheme(tokens: ThemeTokens, root: HTMLElement = document.documentElement): void {
  for (const [name, value] of Object.entries(tokens)) root.style.setProperty(`--${camelToKebab(name)}`, value)
}

// WCAG 2.x 对比度
function luminance(hex: string): number {
  const value = hex.replace('#', '')
  const channels = [0, 2, 4].map(i => parseInt(value.slice(i, i + 2), 16) / 255)
    .map(c => c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
  return 0.2126 * channels[0]! + 0.7152 * channels[1]! + 0.0722 * channels[2]!
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number]
  return (hi + 0.05) / (lo + 0.05)
}

// 界面上实际出现的"文字 / 底色"组合；全部 ≥ 4.5 才算可读
export const TEXT_PAIRS: [keyof ThemeTokens, keyof ThemeTokens][] = [
  ['text', 'bg'], ['text', 'panel'], ['text', 'panel2'], ['muted', 'bg'], ['muted', 'panel'], ['muted', 'panel2'], ['faint', 'panel'],
  ['accentText', 'bg'], ['accentText', 'panel'], ['accentText', 'panel2'], ['accentText', 'rail'], ['accentText', 'input'], ['onAccent', 'accent'], ['ok', 'bg'], ['ok', 'panel'], ['ok', 'panel2'],
  ['warn', 'warnBg'], ['warn', 'bg'], ['warn', 'panel'], ['danger', 'dangerBg'], ['danger', 'bg'], ['danger', 'panel'],
  ['doneFg', 'doneBg'], ['todoFg', 'todoBg'], ['activeFg', 'activeBg'], ['failFg', 'failBg'], ['retryFg', 'retryBg'],
  ['info', 'panel'], ['text', 'input'], ['muted', 'previewInner'], ['text', 'dangerBg'], ['text', 'warnBg'], ['muted', 'warnBg'], ['muted', 'dangerBg']
]

export function readabilityFailures(tokens: ThemeTokens, min = 4.5): string[] {
  return TEXT_PAIRS.filter(([fg, bg]) => contrast(tokens[fg], tokens[bg]) < min)
    .map(([fg, bg]) => `${fg}/${bg} ${contrast(tokens[fg], tokens[bg]).toFixed(2)}`)
}

// 浅色两套都通过检查才保留切换按钮（任务书第三节总则）
export const LIGHT_THEME_AVAILABLE = readabilityFailures(DARK).length === 0 && readabilityFailures(LIGHT).length === 0
