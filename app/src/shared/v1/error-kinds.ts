// error_kind → 给用户看的文案和按钮（约定 v5.2 第 8 节"给用户的处理办法"）。
// 界面上所有失败 / 停止提示都从这里取，不在页面里另写。

export const ERROR_KINDS = [
  'input_invalid', 'key_missing', 'key_invalid', 'balance_insufficient', 'network', 'service_error', 'cost_limit',
  'paid_blocked', 'render_failed', 'disk_full', 'already_running', 'interrupted', 'internal'
] as const

export type ErrorKind = typeof ERROR_KINDS[number]

export type GuideAction =
  | 'resume' // 续跑：run --article，不带 --from
  | 'open_keys' // 去设置 → 密钥
  | 'raise_limit' // 把上限改成提示框里填的金额，然后续跑（APP2：合并成一个操作）
  | 'open_topup' // 打开 RunningHub 首页充值（地址同注册链接常量）
  | 'reimport' // 改好原文后重新导入
  | 'open_cleanup' // 去设置 → 工作文件夹与清理
  | 'copy_diagnostics'
  | 'open_logs'
  | 'dismiss'

export interface GuideButton { label: string; action: GuideAction }

export interface ErrorGuide {
  // tone 决定提示条颜色：danger 红、warn 黄
  tone: 'danger' | 'warn'
  title: string
  text: string
  primary: GuideButton
  secondary?: GuideButton
  // 是否带上引擎的 message 原文（APP2b：所有出错 / 暂停提示都带，默认收起在"查看详情"里；被停止不带）
  showEngineMessage: boolean
  // 到费用上限：提示框里放金额输入框，默认值见 nextLimit
  limitInput?: { defaultValue: number }
}

export interface GuideContext {
  stepName?: string | null
  missingKeys?: string[]
  maxCostCny?: number
  // 失败来自哪家外部服务（约定 v5.3 第 2.8 节 `service`；没有时用通用文案）
  service?: string | null
}

// 约定 v5.3 的服务键 → 界面上的名字
export const SERVICE_NAMES: Record<string, string> = {
  deepseek: 'DeepSeek', ark: '方舟', doubao_speech: '豆包配音', runninghub: 'RunningHub', feishu: '飞书'
}

export function serviceName(service: string | null | undefined): string | null {
  return service ? SERVICE_NAMES[service] ?? null : null
}

// 到费用上限时建议的新上限：当前上限 + 10，向上取整到 10 元
export function nextLimit(current: number): number {
  return Math.ceil((current + 10) / 10) * 10
}

const KEY_LABELS: Record<string, string> = {
  DEEPSEEK_API_KEY: 'DeepSeek', ARK_API_KEY: '方舟', MODEL_SPEECH_API_KEY: '豆包配音', RUNNINGHUB_API_KEY: 'RunningHub',
  FEISHU_APP_ID: '飞书 App ID', FEISHU_APP_SECRET: '飞书 App Secret'
}

const RESUME: GuideButton = { label: '继续（不会重复扣费）', action: 'resume' }

export function errorGuide(kind: string | null | undefined, context: GuideContext = {}): ErrorGuide {
  const where = context.stepName ? `在"${context.stepName}"这一步` : ''
  const limit = context.maxCostCny ?? 50
  const svc = serviceName(context.service)
  switch (kind as ErrorKind) {
    case 'input_invalid':
      return { tone: 'danger', title: '文章有问题，还没开始', text: '按提示改好原文后重新检查。改好之前不会开始，也不会花钱。',
        primary: { label: '改好了，重新检查', action: 'reimport' }, showEngineMessage: true }
    case 'key_missing': {
      const names = (context.missingKeys ?? []).map(key => KEY_LABELS[key] ?? key)
      return { tone: 'danger', title: svc ? `缺少 ${svc} 的密钥` : '缺少密钥', text: `${where}要用的密钥还没填${names.length ? `（${names.join('、')}）` : ''}。还没有发出请求，也没有产生费用。填好后点"继续"。`,
        primary: { label: '去设置填密钥', action: 'open_keys' }, secondary: { label: '已填好，继续', action: 'resume' }, showEngineMessage: true }
    }
    case 'key_invalid':
      return { tone: 'danger', title: svc ? `${svc} 密钥错误` : '密钥错误', text: `${where}被${svc ?? ''}拒绝（密钥错误或没有权限）。可以先到设置里点"检测全部密钥"看看是哪一个，换好后点"继续"。`,
        primary: { label: '去设置换密钥', action: 'open_keys' }, secondary: { label: '已更换，继续', action: 'resume' }, showEngineMessage: true }
    case 'balance_insufficient':
      if (context.service === 'runninghub') {
        return { tone: 'danger', title: 'RunningHub 余额不足，已暂停', text: `${where}RunningHub 账户余额不够了。去 RunningHub 充值后点"已充值，继续"，已经做好的部分不会重复扣费。`,
          primary: { label: '去 RunningHub 充值', action: 'open_topup' }, secondary: { label: '已充值，继续', action: 'resume' }, showEngineMessage: true }
      }
      return { tone: 'danger', title: svc ? `${svc} 余额不足，已暂停` : '余额不足，已暂停', text: `${where}${svc ? `${svc} ` : ''}账户余额不够了。充值后点"已充值，继续"，已经做好的部分不会重复扣费。`,
        primary: { label: '已充值，继续', action: 'resume' }, showEngineMessage: true }
    case 'network':
      return { tone: 'warn', title: svc ? `连不上 ${svc}，已暂停` : '网络不稳定，已暂停', text: '自动重试了几次还是连不上。稍后点"继续"接着做，不会重复扣费。',
        primary: RESUME, showEngineMessage: true }
    case 'service_error':
      return { tone: 'danger', title: svc ? `${svc} 出错` : '外部服务出错', text: '服务返回了错误，原文见下方。可以稍后点"继续"再试；一直不行就把诊断信息发给开发者。',
        primary: RESUME, secondary: { label: '复制诊断信息', action: 'copy_diagnostics' }, showEngineMessage: true }
    case 'cost_limit':
      return { tone: 'warn', title: `已到费用上限 ¥${limit}，暂停`, text: '改一个更高的上限接着做，已经做好的部分不会重复扣费。',
        primary: { label: '上限改成 ¥{N}，继续', action: 'raise_limit' }, limitInput: { defaultValue: nextLimit(limit) }, showEngineMessage: true }
    case 'paid_blocked':
      return { tone: 'warn', title: '这次运行设成了不花钱', text: `${where}需要付费调用，但这次运行不允许花钱。去掉这个限制后点"继续"。`,
        primary: RESUME, showEngineMessage: true }
    case 'render_failed':
      return { tone: 'danger', title: '渲染失败', text: '成片渲染没有成功。点"继续"会重新渲染；还不行就打开日志，把诊断信息发给开发者。',
        primary: RESUME, secondary: { label: '打开日志文件夹', action: 'open_logs' }, showEngineMessage: true }
    case 'disk_full':
      return { tone: 'danger', title: '磁盘空间不足', text: '清理一些空间后点"继续"。可以在"设置 → 工作文件夹与清理"里清掉已完成任务的中间文件。',
        primary: { label: '去清理', action: 'open_cleanup' }, secondary: { label: '已清理，继续', action: 'resume' }, showEngineMessage: true }
    case 'already_running':
      return { tone: 'warn', title: '这篇正在出片', text: '同一篇文章已经在跑了。等它跑完，或者先停掉它。',
        primary: { label: '知道了', action: 'dismiss' }, showEngineMessage: true }
    case 'interrupted':
      return { tone: 'warn', title: '已停止', text: '已经做好的部分都保留着。点"继续"接着做，不会重复扣费。',
        primary: { label: '继续', action: 'resume' }, showEngineMessage: false }
    case 'internal':
    default:
      return { tone: 'danger', title: '出了意外的问题', text: '这不是你操作的问题。把诊断信息发给开发者排查。',
        primary: { label: '复制诊断信息', action: 'copy_diagnostics' }, secondary: { label: '打开日志文件夹', action: 'open_logs' }, showEngineMessage: true }
  }
}

// 约定 1.x / 2.8：退出码 3 = 被停止，4 = 配置或输入问题，2 = 执行失败；0 / 1 不带 error_kind。
export function exitCodeForErrorKind(kind: ErrorKind): 2 | 3 | 4 {
  if (kind === 'interrupted') return 3
  if (kind === 'input_invalid' || kind === 'key_missing') return 4
  return 2
}
