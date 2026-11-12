import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { scanTasks } from './work-scan'

// R120M P2：诊断报告改成一个 .md 文件（文章成片-诊断报告-<时间>.md，默认存桌面）。
// 内容：基本信息、最近 5 个任务（含服务商原始错误信息）、每个失败任务最近一次运行日志最后 120 行和检查报告摘要、
// APP 运行日志最后 200 行。全部内容先脱敏再写：去掉形似密钥 / token 的字符串，用户主目录换成 %USERPROFILE%；
// 写之前再用已存的密钥做一次全文扫描（同 zip 导出），命中就拒绝导出。
// 这里只拼文字、不碰 Electron，方便测试；收集数据（读设置、扫任务）由调用方完成后传进来。

export const RUN_LOG_TAIL = 120
export const APP_LOG_TAIL = 200
export const RECENT_TASKS = 5

export interface ReportBasics {
  appVersion: string
  packaged: boolean
  engineVersion: string | null
  engineStatus: 'ready' | 'missing'
  engineProblems: string[]
  osName: string // 例如 "Windows 11 专业版 10.0.26100"
  osRelease: string
  arch: string
  // 运行环境检查：每项的名字和结果
  runtimeChecks: { name: string; ok: boolean; detail?: string }[]
  imageBackend: 'gpt' | 'moss_local' | 'moss_rh'
  mossStyle: string
  comfyuiUrl: string
  mossFallbackGpt: boolean
  // 只写"已填写 / 未填写"，绝不写值、尾号
  keys: { name: string; filled: boolean }[]
  maxCostCny: number
  lastKeyCheck: { checkedAt: string; statuses: Record<string, string> } | null
}

export interface TaskDigest {
  article: string
  title: string | null
  stateText: string
  updatedAt: string | null
  failedStep: string | null
  failedStepName: string | null
  errorKind: string | null
  service: string | null
  providerMessage: string | null
  engineMessage: string | null
  // 失败任务才有
  runLogName?: string | null
  runLogTail?: string[] | null
  reportSummary?: string[] | null
}

export interface ReportInput {
  generatedAt: Date
  basics: ReportBasics
  tasks: TaskDigest[]
  appLogTail: string[]
  // R121M5：自动更新的情况（没有就不写这一段）
  update?: UpdateDigest | null
}

// R121M5：自动更新的情况：当前版本、检查时间、发现的版本、下载结果、失败原因（失败原因同样先脱敏再写）
export interface UpdateDigest {
  currentVersion: string
  feedOverridden: boolean
  autoCheck: boolean
  lastCheckedAt: string | null
  lastCheckResult: string | null
  foundVersion: string | null
  downloadResult: string | null
  failStage: 'check' | 'download' | 'verify' | 'install' | null
  failReason: string | null
  history: string[]
}

const UPDATE_STAGE_TEXT = { check: '检查更新（连不上更新服务器）', download: '下载', verify: '校验下载的文件', install: '安装' } as const

// --------------------------------------------------------------------------
// 脱敏
// --------------------------------------------------------------------------

export interface RedactOptions {
  homes: string[] // 用户主目录（Windows 的 C:\Users\xxx、Mac 的 /Users/xxx），统一换成 %USERPROFILE%
  secrets: string[] // 已存的密钥值：命中直接替换（扫描兜底仍会拒绝导出）
}

const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

// 脱敏规则（回执附表）：
// 1. 已存的密钥值（及其 base64、URL 编码写法）→ [已隐藏]
// 2. Bearer <值>、api_key / token / secret / authorization / password / app_secret 后跟的值 → [已隐藏]
// 3. sk- 开头的串 → [已隐藏]
// 4. 同时含字母和数字、长度 ≥ 24 的连续字母数字串（形似密钥 / token / 签名）→ [已隐藏]；纯数字（任务号）保留
// 5. 用户主目录（大小写不敏感，正反斜杠两种写法）→ %USERPROFILE%
// 6. 用户名单独出现在 \Users\<名字> 或 /Users/<名字> 里（主目录不在默认位置时）→ %USERNAME%
export function redact(text: string, options: RedactOptions): string {
  let s = text
  for (const secret of options.secrets) {
    if (!secret || secret.length < 6) continue
    for (const form of new Set([secret, Buffer.from(secret, 'utf8').toString('base64').replace(/=+$/, ''), encodeURIComponent(secret)])) {
      s = s.split(form).join('[已隐藏]')
    }
  }
  const homes = [...new Set(options.homes.filter(home => home && home.length > 3))].sort((a, b) => b.length - a.length)
  for (const home of homes) {
    for (const form of new Set([home, home.replace(/\\/g, '/'), home.replace(/\//g, '\\')])) {
      s = s.replace(new RegExp(escapeRe(form), 'gi'), '%USERPROFILE%')
    }
  }
  s = s.replace(/([\\/])Users([\\/])(?!%)([^\\/\s"'`<>|:*?]+)/gi, '$1Users$2%USERNAME%')
  s = s.replace(/\b(bearer)(\s+)[A-Za-z0-9._\-+/=]{6,}/gi, '$1$2[已隐藏]')
  s = s.replace(/\b(authorization|api[_-]?key|access[_-]?token|token|secret|appkey|app_?secret|password)(["']?\s*[:=]\s*["']?|\s+["']?)(?!bearer\b)(?!\[已隐藏\])([A-Za-z0-9._\-+/=]{6,})/gi, '$1$2[已隐藏]')
  s = s.replace(/\bsk-[A-Za-z0-9_-]{8,}/g, '[已隐藏]')
  s = s.replace(/\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{24,}\b/g, '[已隐藏]')
  return s
}

// --------------------------------------------------------------------------
// 读文件的小工具（读不到返回空）
// --------------------------------------------------------------------------

export function tailLines(file: string, count: number): string[] {
  try {
    const lines = readFileSync(file, 'utf8').replace(/^\uFEFF/, '').split(/\r?\n/)
    if (lines.length && lines[lines.length - 1] === '') lines.pop()
    return lines.slice(-count)
  } catch { return [] }
}

// 文章 logs/ 下最近一次运行日志（run-*.log，按修改时间）
export function latestRunLog(articleDir: string): string | null {
  const dir = join(articleDir, 'logs')
  let best: { file: string; mtime: number } | null = null
  try {
    for (const name of readdirSync(dir)) {
      if (!/^run-.*\.log$/.test(name)) continue
      const file = join(dir, name)
      try { const mtime = statSync(file).mtimeMs; if (!best || mtime > best.mtime) best = { file, mtime } } catch { /* 跳过 */ }
    }
  } catch { return null }
  return best?.file ?? null
}

// 检查报告摘要：检查报告.md 的第一行（总体结论）+ 标红 / 提示条目（最多 20 条）
export function reportSummary(articleDir: string): string[] | null {
  try {
    const raw = JSON.parse(readFileSync(join(articleDir, '10-报告', '检查结果.json'), 'utf8').replace(/^\uFEFF/, '')) as { conclusion?: string; items?: { level?: string; message?: string }[]; stale?: boolean }
    const items = Array.isArray(raw.items) ? raw.items : []
    const out = [`总体结论：${raw.conclusion ?? '未知'}${raw.stale ? '（已过期）' : ''}`]
    for (const item of items.slice(0, 20)) out.push(`${item.level === 'red' ? '标红' : '提示'}：${item.message ?? ''}`)
    if (items.length > 20) out.push(`……另有 ${items.length - 20} 条`)
    return out
  } catch { return null }
}

// 账本里失败的那一步：步骤号、错误类型、服务、服务商原话、引擎消息（R120M 起引擎把这几栏记进账本）
export function failedStepOf(articleDir: string): { step: string; name: string | null; errorKind: string | null; service: string | null; providerMessage: string | null; message: string | null } | null {
  try {
    const ledger = JSON.parse(readFileSync(join(articleDir, '进度账本.json'), 'utf8').replace(/^\uFEFF/, '')) as { steps?: Record<string, unknown>[] }
    const str = (v: unknown): string | null => typeof v === 'string' && v.trim() ? v.trim() : null
    const step = (ledger.steps ?? []).find(item => item.status === 'failed')
    if (!step) return null
    return { step: String(step.step), name: str(step.name), errorKind: str(step.error_kind), service: str(step.service), providerMessage: str(step.provider_message), message: str(step.message) }
  } catch { return null }
}

// --------------------------------------------------------------------------
// 拼报告
// --------------------------------------------------------------------------

const fence = (lines: string[]): string[] => {
  const body = lines.length ? lines : ['（空）']
  const longest = Math.max(3, ...body.map(line => (line.match(/`+/g) ?? []).reduce((m, x) => Math.max(m, x.length + 1), 0)))
  const f = '`'.repeat(longest)
  return [f + 'text', ...body, f]
}

const cell = (value: string | null | undefined): string => (value ?? '—').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ')

const stamp = (d: Date): string => {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function reportFileName(d: Date): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `文章成片-诊断报告-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.md`
}

export function buildDiagnosticsReport(input: ReportInput, options: RedactOptions): string {
  const b = input.basics
  const out: string[] = []
  out.push('# 文章成片 · 诊断报告', '', `生成时间：${stamp(input.generatedAt)}`, '',
    '> 本报告不含任何密钥、密钥尾号；用户文件夹路径已替换为 %USERPROFILE%。可以直接发给作者。', '')
  out.push('## 一、基本信息', '', '| 项 | 值 |', '|---|---|',
    `| APP 版本 | ${cell(b.appVersion)}（${b.packaged ? '安装版' : '开发模式'}） |`,
    `| 引擎版本 | ${cell(b.engineVersion ?? '未知')} |`,
    `| 系统 | ${cell(b.osName)}（${cell(b.osRelease)}，${cell(b.arch)}） |`,
    `| 运行环境 | ${b.engineStatus === 'ready' ? '正常' : '有问题：' + cell(b.engineProblems.join('；'))} |`,
    `| 出图方式 | ${b.imageBackend === 'moss_local' ? 'MOSS 本地（ComfyUI）' : b.imageBackend === 'moss_rh' ? 'MOSS · RH 云端' : 'GPT（RunningHub）'} |`,
    ...(b.imageBackend === 'moss_local'
      ? [`| MOSS 风格 | ${cell(b.mossStyle)} |`, `| ComfyUI 地址 | ${cell(b.comfyuiUrl)} |`, `| MOSS 不合格时用 GPT 补图 | ${b.mossFallbackGpt ? '开' : '关'} |`]
      : b.imageBackend === 'moss_rh'
        ? [`| MOSS 风格 | ${cell(b.mossStyle)} |`, `| RH 同时出图数 | 自动（起始 3，RunningHub 排队时降为 1） |`, `| MOSS 不合格时用 GPT 补图 | ${b.mossFallbackGpt ? '开' : '关'} |`]
        : []),
    `| 默认费用上限 | ¥${b.maxCostCny} |`, '')
  out.push('**运行环境检查**', '')
  for (const check of b.runtimeChecks) out.push(`- ${check.ok ? '✅' : '❌'} ${check.name}${check.detail ? `：${check.detail}` : ''}`)
  out.push('', '**密钥**（只写是否填写）', '')
  for (const key of b.keys) out.push(`- ${key.name}：${key.filled ? '已填写' : '未填写'}`)
  if (b.lastKeyCheck) {
    out.push('', `上次检测密钥（${b.lastKeyCheck.checkedAt}）：${Object.entries(b.lastKeyCheck.statuses).map(([k, v]) => `${k}=${v}`).join('，') || '无'}`)
  }
  if (input.update) {
    const u = input.update
    out.push('', '**自动更新**', '', '| 项 | 值 |', '|---|---|',
      `| 当前版本 | ${cell(u.currentVersion)} |`,
      `| 启动时自动检查 | ${u.autoCheck ? '开' : '关'} |`,
      `| 更新源 | ${u.feedOverridden ? '测试地址（环境变量 A2V_UPDATE_FEED_URL）' : 'GitHub Release（默认）'} |`,
      `| 上次检查时间 | ${cell(u.lastCheckedAt ?? '还没检查过')} |`,
      `| 上次检查结果 | ${cell(u.lastCheckResult)} |`,
      `| 发现的版本 | ${cell(u.foundVersion)} |`,
      `| 下载结果 | ${cell(u.downloadResult)} |`,
      `| 失败环节 | ${cell(u.failStage ? UPDATE_STAGE_TEXT[u.failStage] : null)} |`,
      `| 失败原因 | ${cell(u.failReason)} |`)
    if (u.history.length) {
      out.push('', '最近的更新记录：', '', '```')
      for (const line of u.history) out.push(line.replace(/```/g, "'''"))
      out.push('```')
    }
  }
  out.push('')

  out.push(`## 二、最近 ${RECENT_TASKS} 个任务`, '')
  if (!input.tasks.length) out.push('工作文件夹里还没有任务。', '')
  else {
    out.push('| 文章 | 状态 | 停在哪一步 | 错误类型 | 服务 | 更新时间 |', '|---|---|---|---|---|---|')
    for (const t of input.tasks) {
      out.push(`| ${cell(t.title ?? t.article)} | ${cell(t.stateText)} | ${cell(t.failedStep ? `${t.failedStep}${t.failedStepName ? ' ' + t.failedStepName : ''}` : null)} | ${cell(t.errorKind)} | ${cell(t.service)} | ${cell(t.updatedAt)} |`)
    }
    out.push('')
    for (const t of input.tasks) {
      if (!t.providerMessage && !t.engineMessage) continue
      out.push(`**${t.title ?? t.article}**`, '')
      if (t.providerMessage) out.push(`- 服务商返回的原始错误信息：${t.providerMessage}`)
      if (t.engineMessage) out.push(`- 引擎消息：${t.engineMessage}`)
      out.push('')
    }
  }

  const failed = input.tasks.filter(t => t.failedStep)
  out.push('## 三、失败任务的日志', '')
  if (!failed.length) out.push('最近的任务里没有失败的。', '')
  for (const t of failed) {
    out.push(`### ${t.title ?? t.article}`, '', `文件夹：${t.article}`, '')
    out.push(`**最近一次运行日志**（${t.runLogName ?? '没有找到运行日志'}，最后 ${RUN_LOG_TAIL} 行）`, '', ...fence(t.runLogTail ?? []), '')
    out.push('**检查报告摘要**', '')
    if (t.reportSummary?.length) for (const line of t.reportSummary) out.push(`- ${line}`)
    else out.push('- 还没有检查报告（失败在生成检查报告之前）')
    out.push('')
  }

  out.push(`## 四、APP 运行日志（最后 ${APP_LOG_TAIL} 行）`, '', ...fence(input.appLogTail), '')
  return redact(out.join('\n'), options)
}

// 扫描兜底：报告里不能出现任何已存密钥的任一写法
export function reportLeaks(text: string, secrets: string[]): boolean {
  for (const secret of secrets) {
    if (!secret || secret.length < 6) continue
    for (const form of [secret, Buffer.from(secret, 'utf8').toString('base64').replace(/=+$/, ''), encodeURIComponent(secret)]) {
      if (text.includes(form)) return true
    }
  }
  return false
}

// --------------------------------------------------------------------------
// 收集最近的任务（工作文件夹扫描，只读）
// --------------------------------------------------------------------------

export function collectRecentTasks(workDir: string, activeArticle: string | null, count = RECENT_TASKS): TaskDigest[] {
  return scanTasks(workDir, activeArticle).slice(0, count).map(row => {
    const dir = join(workDir, row.article)
    const failed = row.state === 'failed' ? failedStepOf(dir) : null
    const digest: TaskDigest = {
      article: row.article, title: row.title, stateText: row.stateText, updatedAt: row.updatedAt,
      failedStep: failed?.step ?? null, failedStepName: failed?.name ?? null, errorKind: failed?.errorKind ?? null,
      service: failed?.service ?? null, providerMessage: failed?.providerMessage ?? null, engineMessage: failed?.message ?? null
    }
    if (failed) {
      const log = latestRunLog(dir)
      digest.runLogName = log ? log.split(/[\\/]/).pop() ?? null : null
      digest.runLogTail = log ? tailLines(log, RUN_LOG_TAIL) : null
      digest.reportSummary = reportSummary(dir)
    }
    return digest
  })
}
