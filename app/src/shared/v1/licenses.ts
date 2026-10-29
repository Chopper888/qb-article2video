import type { ExternalLinkId } from './links'

// APP3：设置 → 关于与诊断 → 第三方组件。只列事实：名称、版本、许可；
// available 为 false 表示这台机器上找不到随附的许可文件（点不开）。
export interface ThirdPartyComponent {
  id: string
  name: string
  version: string
  license: string
  available: boolean
  // 官方许可页（只放 links.ts 白名单里的）
  officialLink?: ExternalLinkId
}
