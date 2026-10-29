import type { DemoImportScenario, DemoRunScenario } from '../../shared/v1/adapter'
import type { CheckReport, EngineEvent } from '../../shared/v1/protocol'
import { SCREENSHOT_STEPS, THEORY_STEPS, type ContentType } from '../../shared/v1/steps'

// 演示数据：按约定 v5.3 的事件格式拼出来的固定序列。不是引擎输出，不联网、不花钱。

export interface DemoStep { delayMs: number; event: EngineEvent }
export interface DemoScript {
  steps: DemoStep[]
  // true：事件放完以后不发 run_done，像引擎还在跑；按"停止"才收尾（退出码 3）
  openEnded: boolean
  report: CheckReport | null
}

export const DEMO_ARTICLES = {
  screenshot: { article: '示例-演示文章A', title: '示例演示文章：如何整理电脑桌面', source: 'feishu' as const, images: 14 },
  theory: { article: '示例-演示文章B', title: '示例演示文章：几种常用工具的搭配', source: 'feishu' as const, images: 6 },
  problems: { article: '示例-演示文章C', title: '示例演示文章：日常效率小技巧', source: 'markdown' as const, images: 12 }
}

// 费用确认页"章节"列表的演示数据（演示场景的文章不在工作文件夹里）
export const DEMO_CHAPTERS = { total: 9, items: [
  { level: 1 as const, title: '示例演示文章：如何整理电脑桌面' },
  { level: 2 as const, title: '一、为什么要整理桌面' }, { level: 2 as const, title: '二、准备工作' },
  { level: 2 as const, title: '三、先按用途分类' }, { level: 2 as const, title: '四、处理重复和旧文件' },
  { level: 2 as const, title: '五、给文件命名' }, { level: 2 as const, title: '六、常见问题' },
  { level: 2 as const, title: '七、进阶：定期归档' }, { level: 2 as const, title: '八、总结' }
] }

class Seq {
  private seq = 0
  private clock = Date.parse('2026-10-23T10:00:00+08:00')
  readonly out: DemoStep[] = []
  constructor(private readonly delayMs: number) {}

  push(event: string, step: string | null, message: string, extra: Record<string, unknown> = {}, delay = this.delayMs): void {
    this.seq += 1
    this.clock += 13_000
    this.out.push({ delayMs: delay, event: { schema_version: 1, seq: this.seq, time: new Date(this.clock).toISOString(), event, step, message, ...extra } as unknown as EngineEvent })
  }

  progress(step: string, done: number, total: number, unit: string, message = ''): void {
    this.push('step_progress', step, message, { done, total, unit })
  }
}

interface Article { article: string; title: string; source: 'feishu' | 'markdown'; images: number }

function resolved(s: Seq, a: Article, type: ContentType | null): void {
  s.push('input_resolved', null, `已导入：${a.title}`, {
    article: a.article, work_dir: `/演示/工作/${a.article}`, source_type: a.source, title: a.title,
    reused: false, changed: false, image_count: a.images, content_type: type, warnings: []
  })
}

// 按顺序跑完 from 之前的步骤（带上各自的步骤内进度和费用）
const COSTS: Record<string, number> = { S0: 0.4, S1: 1.1, S3: 2.6, S6: 3.2, S7: 1.6, S8: 0 }

function runStep(s: Seq, id: string, name: string, type: ContentType, opts: { imagesTotal: number; costs?: Record<string, number> }): void {
  s.push('step_start', id, `${name}（第 1 次）`, { name })
  if (id === 'S0') { for (let i = 1; i <= 14; i += 7) s.progress(id, Math.min(i + 6, 14), 14, '张') }
  if (id === 'S3') { s.progress(id, 0, 1, '段'); s.progress(id, 1, 1, '段') }
  if (id === 'S6') for (let i = 1; i <= 4; i += 1) s.progress(id, i, 4, '项')
  if (id === 'S7' && type === 'screenshot') for (const n of [8, 16, 23]) s.progress(id, n, 23, '张', '截图标注')
  if (id === 'S8') for (const n of [40, 80, 120].filter(n => n < opts.imagesTotal).concat(opts.imagesTotal)) s.progress(id, n, opts.imagesTotal, '张')
  if (id === 'S10') for (const n of [30, 60, 90, 99, 100]) s.progress(id, n, 100, '%', n <= 90 ? '渲染' : '编码')
  const cost = opts.costs?.[id] ?? (id === 'S8' ? Math.round(opts.imagesTotal * 0.09 * 100) / 100 : (COSTS[id] ?? 0))
  s.push('step_done', id, `${name}完成`, { name, estimated_cny: cost, ...(id === 'S0' ? { content_type: type } : {}) })
}

function runUntil(s: Seq, type: ContentType, stop: string, imagesTotal: number, costs?: Record<string, number>): void {
  const steps = type === 'screenshot' ? SCREENSHOT_STEPS : THEORY_STEPS
  for (const step of steps) {
    if (step.id === stop) return
    runStep(s, step.id, step.name, type, { imagesTotal, costs })
  }
}

// 到费用上限的演示：已花费接近 ¥50（APP2 第一部分第 8 项）
const NEAR_LIMIT_COSTS: Record<string, number> = { S0: 0.4, S1: 1.1, S3: 2.6, S6: 23.9, S7: 21.8 }

function startAndPartial(s: Seq, id: string, name: string, unit: string, upTo: number[], total: number, message = ''): void {
  s.push('step_start', id, `${name}（第 1 次）`, { name })
  for (const n of upTo) s.progress(id, n, total, unit, message)
}

function fail(s: Seq, a: Article, step: string, kind: string, message: string, extra: Record<string, unknown> = {}): void {
  s.push('step_fail', step, message, { error_kind: kind, ...extra })
  s.push('run_done', null, message, { exit_code: 2, reason: 'step_failed', article: a.article, error_kind: kind, ...extra })
}

const NOTICES: CheckReport['items'] = [
  { level: 'notice', kind: 'layout_tip', message: '正文有 2 处像"导览"的段落，已按第 5 组处理', time: null, detail_file: null },
  { level: 'notice', kind: 'early_appearance', message: '一张插画比它的关键句早出现了 7 秒', time: '06:30', detail_file: 'logs/提前出现核对表.json' }
]

function report(verdict: CheckReport['verdict'], items: CheckReport['items']): CheckReport {
  return {
    schema_version: 1, verdict, conclusion: verdict === 'pass' ? '通过' : verdict === 'review' ? '有提示' : '标红', rendered: true, items,
    generated_at: '2026-10-23T11:12:40+08:00', generated_by: 'S10', stale: false, stale_detail: null
  }
}

export function importScript(scenario: DemoImportScenario, delayMs: number): DemoScript {
  const s = new Seq(delayMs)
  if (scenario === 'import_problems') {
    const a = DEMO_ARTICLES.problems
    resolved(s, a, null)
    s.push('input_problems', null, '输入有 3 处问题', {
      problems: [
        { kind: 'image_missing', line: 23, detail: '第 23 行：![](图片和附件/安装界面.png)', hint: '把「安装界面.png」放进「图片和附件」文件夹，或检查文件名是否写错。' },
        { kind: 'image_remote_url', line: 41, detail: '第 41 行：![](https://example.com/shot.png)', hint: '先把图片下载到「图片和附件」文件夹，再改成本地路径。' },
        { kind: 'image_format_unsupported', line: 58, detail: '第 58 行：![](图片和附件/IMG_0231.HEIC)', hint: '在电脑上另存为 PNG 或 JPG，再替换引用。' }
      ]
    })
    s.push('run_done', null, '输入有阻断问题', { exit_code: 4, reason: 'config_error', article: a.article, error_kind: 'input_invalid' })
    return { steps: s.out, openEnded: false, report: null }
  }
  const a = DEMO_ARTICLES.screenshot
  resolved(s, a, null)
  s.push('run_done', null, '导入完成', { exit_code: 0, article: a.article, report_json: null })
  return { steps: s.out, openEnded: false, report: null }
}

export function estimateScript(scenario: DemoImportScenario, delayMs: number): DemoScript {
  const s = new Seq(delayMs)
  const over = scenario === 'import_over_limit'
  const [low, high] = over ? [28, 62] : [15, 30]
  const illus: [number, number] = over ? [260, 420] : [110, 220]
  s.push('estimate', null, '预计费用', {
    low_cny: low, high_cny: high, saved_by_cache_cny: 0,
    by_service: {
      runninghub: { service_name: 'RunningHub 出图', low_cny: over ? 22 : 12, high_cny: over ? 52 : 23, price_estimated: true },
      doubao_speech: { service_name: '豆包语音合成', low_cny: 3, high_cny: 3, price_estimated: false },
      deepseek: { service_name: 'DeepSeek', low_cny: over ? 2 : 0, high_cny: over ? 5 : 3, price_estimated: false },
      ark: { service_name: '方舟识图', low_cny: over ? 1 : 0, high_cny: over ? 2 : 1, price_estimated: true }
    },
    basis: { chars: over ? 21300 : 9900, images: 14, content_type: null, content_type_basis: '推测', illustrations: illus }
  })
  s.push('run_done', null, '估算完成', { exit_code: 0, article: DEMO_ARTICLES.screenshot.article })
  return { steps: s.out, openEnded: false, report: null }
}

export function runScript(scenario: DemoRunScenario, delayMs: number): DemoScript {
  const s = new Seq(delayMs)
  const shot = DEMO_ARTICLES.screenshot
  switch (scenario) {
    case 'run_theory': {
      const a = DEMO_ARTICLES.theory
      resolved(s, a, 'theory')
      runUntil(s, 'theory', 'S8', 86)
      startAndPartial(s, 'S8', '出图', '张', [20, 40, 62], 86)
      return { steps: s.out, openEnded: true, report: null }
    }
    case 'run_screenshot':
    case 'run_render': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S10', 160)
      startAndPartial(s, 'S10', '渲染成片', '%', [12, 30, 47], 100, '渲染')
      return { steps: s.out, openEnded: true, report: null }
    }
    case 'network_retry': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S8', 160)
      startAndPartial(s, 'S8', '出图', '张', [40, 87], 160)
      s.push('step_progress', 'S8', '网络不稳定，正在重试（第 2 次）', { done: 87, total: 160, unit: '张',
        retry: { service: 'runninghub', what: '出图', attempt: 2, wait_sec: 45, reason: 'HTTP 503' } })
      return { steps: s.out, openEnded: true, report: null }
    }
    case 'balance_insufficient': {
      // 约定 v5.3：RunningHub 余额不足 = balance_insufficient + service=runninghub
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S8', 160)
      startAndPartial(s, 'S8', '出图', '张', [40, 80, 96], 160)
      fail(s, shot, 'S8', 'balance_insufficient', 'RunningHub 提交失败 HTTP 200: {"errorCode":"605","errorMessage":"Your balance is insufficient. Please top up your account."}', { service: 'runninghub' })
      return { steps: s.out, openEnded: false, report: null }
    }
    case 'key_invalid': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S1', 160)
      s.push('step_start', 'S1', '口播改写（第 1 次）', { name: '口播改写' })
      fail(s, shot, 'S1', 'key_invalid', 'DeepSeek 口播失败（密钥错误或无权限）: HTTP 401', { service: 'deepseek' })
      return { steps: s.out, openEnded: false, report: null }
    }
    case 'cost_limit': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S8', 160, NEAR_LIMIT_COSTS)
      startAndPartial(s, 'S8', '出图', '张', [40, 80, 120], 160)
      fail(s, shot, 'S8', 'cost_limit', '超出费用上限：已花 ¥49.80，再出一张会超过上限 ¥50', { service: null })
      return { steps: s.out, openEnded: false, report: null }
    }
    case 'stopped': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S8', 160)
      startAndPartial(s, 'S8', '出图', '张', [40, 62], 160)
      s.push('run_done', null, '已停止', { exit_code: 3, reason: 'interrupted', article: shot.article, error_kind: 'interrupted' })
      return { steps: s.out, openEnded: false, report: null }
    }
    case 'missing_images': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S11', 160)
      const r = report('fail', [
        { level: 'red', kind: 'illustration_missing', message: '第 G12 组插画多次没出来，已用前后画面补位', time: '03:12', detail_file: '07-出图/缺图.json' },
        { level: 'red', kind: 'illustration_missing', message: '第 G31 组插画多次没出来，已用前后画面补位', time: '11:47', detail_file: '07-出图/缺图.json' },
        ...NOTICES
      ])
      s.push('run_done', null, '已出片，有标红', { exit_code: 1, article: shot.article, report_json: `/演示/工作/${shot.article}/10-报告/检查结果.json`, delivery_path: `/演示/成片/${shot.title}.mp4` })
      return { steps: s.out, openEnded: false, report: r }
    }
    case 'done_pass': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S11', 160)
      s.push('run_done', null, '已出片', { exit_code: 0, article: shot.article, report_json: `/演示/工作/${shot.article}/10-报告/检查结果.json`, delivery_path: `/演示/成片/${shot.title}.mp4` })
      return { steps: s.out, openEnded: false, report: report('review', NOTICES) }
    }
    case 'done_red': {
      resolved(s, shot, 'screenshot')
      runUntil(s, 'screenshot', 'S11', 160)
      const r = report('fail', [
        { level: 'red', kind: 'tts_missing', message: '配音漏读了一句："把这个文件夹拖进 obsidian 的库里"', time: '02:41', detail_file: 'logs/配音完整性核对表.json' },
        { level: 'red', kind: 'element_not_on_screen', message: '截图里的"新建技能"按钮没有出现在画面上', time: '08:05', detail_file: 'logs/要素覆盖核对表.json' },
        ...NOTICES,
        { level: 'notice', kind: 'annotation_gap', message: '截图标注中间空了 9 秒', time: '14:20', detail_file: 'logs/标注登记.json' }
      ])
      s.push('run_done', null, '已出片，有标红', { exit_code: 1, article: shot.article, report_json: `/演示/工作/${shot.article}/10-报告/检查结果.json`, delivery_path: `/演示/成片/${shot.title}.mp4` })
      return { steps: s.out, openEnded: false, report: r }
    }
  }
}

// 演示的"按停止"：引擎收到停止后发一条 run_done（退出码 3）
export function stopEvent(article: string, lastSeq: number): EngineEvent {
  return { schema_version: 1, seq: lastSeq + 1, time: new Date().toISOString(), event: 'run_done', step: null, message: '已停止',
    exit_code: 3, reason: 'interrupted', article, error_kind: 'interrupted' } as EngineEvent
}
