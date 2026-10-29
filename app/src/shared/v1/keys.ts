import type { ExternalLinkId } from './links'

// 五项服务、六个密钥字段（约定 1.4 节）。字段 id 只在 APP 内部用；交给引擎时换成环境变量名。

export type KeyFieldId = 'deepseek' | 'tts' | 'ark' | 'runninghub' | 'feishu_app_id' | 'feishu_app_secret'
export type KeyServiceId = 'deepseek' | 'tts' | 'ark' | 'runninghub' | 'feishu'

export const KEY_ENV_VARS: Record<KeyFieldId, string> = {
  deepseek: 'DEEPSEEK_API_KEY',
  ark: 'ARK_API_KEY',
  tts: 'MODEL_SPEECH_API_KEY',
  runninghub: 'RUNNINGHUB_API_KEY',
  feishu_app_id: 'FEISHU_APP_ID',
  feishu_app_secret: 'FEISHU_APP_SECRET'
}

export const KEY_FIELD_IDS = Object.keys(KEY_ENV_VARS) as KeyFieldId[]

export interface KeyServiceDef {
  id: KeyServiceId
  name: string
  purpose: string
  fields: { id: KeyFieldId; label: string }[]
  optionalNote?: string
  // APP4："去哪里拿"打开的官方页面（RunningHub 用注册链接常量）
  keyLink: ExternalLinkId
  // APP4：密钥行下面的小字说明
  howNote?: string
}

// 显示顺序与原型一致
export const KEY_SERVICES: KeyServiceDef[] = [
  { id: 'deepseek', name: 'DeepSeek', purpose: '整理文章、写口播、编排画面', fields: [{ id: 'deepseek', label: 'API Key' }], keyLink: 'deepseekKeys' },
  { id: 'tts', name: '豆包配音', purpose: '把口播配成语音', fields: [{ id: 'tts', label: 'API Key' }], keyLink: 'ttsKeys' },
  { id: 'ark', name: '方舟', purpose: '识别截图和配图', fields: [{ id: 'ark', label: 'API Key' }], keyLink: 'arkKeys' },
  { id: 'runninghub', name: 'RunningHub', purpose: '生成插图', fields: [{ id: 'runninghub', label: 'API Key' }], keyLink: 'runninghubSignup' },
  { id: 'feishu', name: '飞书应用', purpose: '读取飞书文章', optionalNote: '只用本地 Markdown 可以不填',
    fields: [{ id: 'feishu_app_id', label: 'App ID' }, { id: 'feishu_app_secret', label: 'App Secret' }], keyLink: 'feishuApps',
    howNote: '需要在飞书开放平台创建应用，并把要导入的文档授权给这个应用' }
]

export interface KeyFieldStatus { filled: boolean; last4: string | null }
export type KeyStatusMap = Record<KeyFieldId, KeyFieldStatus>

// 一项服务所有字段都填了才算"已配置"（飞书要 App ID 和 App Secret 都有）
export function serviceConfigured(service: KeyServiceDef, status: KeyStatusMap): boolean {
  return service.fields.every(field => status[field.id]?.filled)
}

export function configuredCount(status: KeyStatusMap): { configured: number; total: number } {
  return { configured: KEY_SERVICES.filter(service => serviceConfigured(service, status)).length, total: KEY_SERVICES.length }
}

// 引擎 check-keys 的服务键与 APP 服务 id 相同
export const CHECK_SERVICE_IDS: KeyServiceId[] = ['deepseek', 'ark', 'tts', 'runninghub', 'feishu']
