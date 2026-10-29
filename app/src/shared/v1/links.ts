// RunningHub 注册入口。界面上所有"去注册"链接都只用这一个常量，由 Main 用系统浏览器打开。
export const RUNNINGHUB_SIGNUP_URL = 'https://www.runninghub.ai?inviteCode=rh-v1419'

// Main 只允许用系统浏览器打开这里列出的地址，页面不能传任意网址。
export const EXTERNAL_LINKS = {
  runninghubSignup: RUNNINGHUB_SIGNUP_URL,
  // APP3：关于页"第三方组件"里 Remotion 的官方许可
  remotionLicense: 'https://www.remotion.dev/license',
  // APP4：密钥行的"去哪里拿"——各家官方控制台里获取密钥的页面（地址出处见 APP4 回执）
  deepseekKeys: 'https://platform.deepseek.com/api_keys',
  ttsKeys: 'https://console.volcengine.com/speech/new/setting/apikeys',
  arkKeys: 'https://ark.volcengine.com/region:cn-beijing/apiKey',
  feishuApps: 'https://open.feishu.cn/app'
} as const

export type ExternalLinkId = keyof typeof EXTERNAL_LINKS
