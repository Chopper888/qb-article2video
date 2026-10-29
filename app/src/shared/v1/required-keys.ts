import { KEY_SERVICES, serviceConfigured, type KeyServiceId, type KeyStatusMap } from './keys'

// 本次出片一定要的密钥（约定第 8 节"密钥按需要求"，任务书 APP2 第一部分第 5 项）：
//   - DeepSeek、豆包配音、RunningHub 一定要；
//   - 飞书链接要飞书两项；
//   - 方舟：文章有图时要（有图且没指定类型时 S0 要识图；判成截图型后 S6 也要方舟）。拿不准按"需要"处理。

export interface RequiredKeysInput {
  sourceType: 'feishu' | 'markdown' | string | null | undefined
  imageCount: number | null | undefined
}

export function requiredServices(input: RequiredKeysInput): KeyServiceId[] {
  const need: KeyServiceId[] = ['deepseek', 'tts', 'runninghub']
  // 图片数读不到时按"有图"处理
  if (input.imageCount === null || input.imageCount === undefined || input.imageCount > 0) need.push('ark')
  if (input.sourceType === 'feishu') need.push('feishu')
  return need
}

// 必需但没配置好的服务（按设置页的显示顺序）
export function missingRequiredServices(input: RequiredKeysInput, status: KeyStatusMap): KeyServiceId[] {
  const need = new Set(requiredServices(input))
  return KEY_SERVICES.filter(service => need.has(service.id) && !serviceConfigured(service, status)).map(service => service.id)
}

export function missingKeysText(missing: KeyServiceId[]): string {
  const names = missing.map(id => KEY_SERVICES.find(service => service.id === id)?.name ?? id)
  return `还差 ${names.join('、')} 的密钥，去填写`
}
