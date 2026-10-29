import { Check, LoaderCircle } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ChapterList } from '../../../shared/v1/chapters'
import type { EstimateEvent, InputProblem, InputResolvedEvent } from '../../../shared/v1/protocol'
import { unescapeMarkdown } from '../../../shared/v1/title'
import { EngineDetail } from '../parts'

// 导入检查与费用确认（原型 G、D 第二屏）。问题清单字段按约定 2.2 节，费用按 2.6 节。

const PROBLEM_LABELS: Record<string, [string, (line?: number) => string]> = {
  encoding_invalid: ['编码不对', () => '文章不是 UTF-8 编码'],
  image_missing: ['找不到图片', line => `第 ${line ?? '?'} 行引用的图片不存在`],
  image_outside_folder: ['位置不对', line => `第 ${line ?? '?'} 行的图片在文章文件夹外面`],
  image_absolute_path: ['绝对路径', line => `第 ${line ?? '?'} 行用了电脑上的完整路径`],
  image_remote_url: ['网络图片', line => `第 ${line ?? '?'} 行是网络图片，成片里不会显示`],
  image_title_syntax: ['写法不支持', line => `第 ${line ?? '?'} 行的图片带了标题写法`],
  image_html_tag: ['HTML 图片', line => `第 ${line ?? '?'} 行用了 <img> 标签`],
  image_format_unsupported: ['格式不支持', line => `第 ${line ?? '?'} 行的图片格式不支持`],
  unhandled_block: ['不会进成片', () => '飞书里的视频、附件或嵌入表格'],
  // APP4（约定 v5.5）
  image_fallback: ['用了同名图片', line => `第 ${line ?? '?'} 行的路径找不到，改用了文件夹里唯一的同名图片`],
  zip_corrupt: ['压缩包打不开', () => '压缩包损坏、加了密码或不是 zip'],
  zip_unsafe_path: ['压缩包不安全', () => '压缩包里有指向外面的路径'],
  zip_no_md: ['没有文章', () => '压缩包里没有 md 文件'],
  zip_multiple_md: ['多篇文章', () => '压缩包里有多个 md 文件'],
  // 1.0.1（R101 L）：引擎的 zip 导入上限
  zip_too_large: ['压缩包太大', () => '压缩包太大（超过 2 GB），请删掉不需要的文件后重试']
}

export function problemLabel(problem: InputProblem): { tag: string; what: string } {
  const entry = PROBLEM_LABELS[problem.kind]
  return entry ? { tag: entry[0], what: entry[1](problem.line) } : { tag: '有问题', what: problem.detail }
}

// APP4：导入提示（不影响开始）。同名兜底可能很多条（拖进 md 加图片时每张图一条），超过 2 条合并成一条，点开看明细
export function WarningList({ warnings }: { warnings: InputProblem[] }): React.JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (!warnings.length) return null
  const fallback = warnings.filter(warning => warning.kind === 'image_fallback')
  const others = warnings.filter(warning => warning.kind !== 'image_fallback')
  const merged = fallback.length > 2
  return (
    <>
      {others.concat(merged ? [] : fallback).map((warning, index) => <div className="warn-card" key={index}><span>提示（不影响开始）：{warning.detail}。{warning.hint}</span></div>)}
      {merged && (
        <div className="warn-card">
          <span>提示（不影响开始）：有 {fallback.length} 张图片按 md 里写的路径找不到。{fallback[0]!.hint}
            {' '}<button className="link" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起明细' : '查看明细'}</button></span>
          {open && <ul className="warn-detail">{fallback.map((warning, index) => <li key={index} className="mono">{warning.detail}</li>)}</ul>}
        </div>
      )}
    </>
  )
}

export function Loading({ text }: { text: string }): React.JSX.Element {
  return <div className="center-fill"><LoaderCircle className="spin" size={18} style={{ marginRight: 8 }} />{text}</div>
}

export function ProblemsView({ resolved, problems, engineMessage, onOpenFolder, onRecheck }: { resolved: InputResolvedEvent | null; problems: InputProblem[]; engineMessage?: string | null; onOpenFolder(): void; onRecheck(): void }): React.JSX.Element {
  const warnings = resolved?.warnings ?? []
  return (
    <div className="import-page">
      <div className="import-col">
        <div className="problem-head">
          <span className="mark">!</span>
          <div>
            <h2>{problems.length ? `这篇文章有 ${problems.length} 处问题，改好之前不会开始，也不会花钱` : '文章没有导入成功，还没开始，也没有花钱'}</h2>
            {resolved
              ? <div className="muted">{`文章：${unescapeMarkdown(resolved.title)} · ${resolved.source_type === 'markdown' ? '本地 Markdown' : '飞书'} · 图片 ${resolved.image_count} 张`}</div>
              : <EngineDetail message={engineMessage ?? '没有返回原因'} />}
          </div>
        </div>
        <div className="problems">
          {problems.map((problem, index) => {
            const label = problemLabel(problem)
            return (
              <div className="problem" key={index}>
                <span className="tag">{label.tag}</span>
                <div>
                  <div className="what">{label.what}</div>
                  <code className="mono">{problem.detail}</code>
                  <div className="hint">怎么改：{problem.hint}</div>
                </div>
              </div>
            )
          })}
        </div>
        <WarningList warnings={warnings} />
        <div className="import-foot">
          <span>图片放在文章文件夹里就行（和 md 同一层或任意子文件夹），支持 png / jpg / webp / gif。</span>
          <span className="acts"><button className="btn btn-lg" onClick={onOpenFolder}>打开文章文件夹</button><button className="btn btn-primary btn-lg" onClick={onRecheck}>改好了，重新检查</button></span>
        </div>
      </div>
    </div>
  )
}

const yuan = (value: number): string => `¥${Math.round(value)}`

export interface ConfirmProps {
  resolved: InputResolvedEvent
  estimate: EstimateEvent | null
  defaultMaxCost: number
  outputDir: string
  keysLine: { text: string; ok: boolean }
  runninghubLine: { text: string; tone: string }
  // 本次出片必需但还没填的密钥（约定第 8 节"密钥按需要求"）；有缺的就不能开始
  missingKeysText: string | null
  onOpenKeys(): void
  chapters: ChapterList | null
  onBack(): void
  onConfirm(maxCostCny: number): void
  // APP3：运行环境还没准备好时不能开始；runtimeNotice 是准备进度提示
  runtimeBusy?: boolean
  runtimeNotice?: React.ReactNode
}

export function ConfirmView({ resolved, estimate, defaultMaxCost, outputDir, keysLine, runninghubLine, missingKeysText, onOpenKeys, chapters, onBack, onConfirm, runtimeBusy = false, runtimeNotice }: ConfirmProps): React.JSX.Element {
  const [limit, setLimit] = useState(defaultMaxCost)
  const over = !!estimate && estimate.high_cny > defaultMaxCost
  const suggested = estimate ? Math.ceil(estimate.high_cny / 10) * 10 : defaultMaxCost
  const [raiseTo, setRaiseTo] = useState(suggested)
  const [dialog, setDialog] = useState(over)
  useEffect(() => { setDialog(over); setRaiseTo(suggested) }, [over, suggested])
  const images = estimate?.by_service.runninghub
  const voice = estimate?.by_service.doubao_speech
  const illus = estimate?.basis.illustrations
  const range = estimate ? `${yuan(estimate.low_cny)}～${Math.round(estimate.high_cny)}` : '估算中…'
  const blocked = !!missingKeysText || runtimeBusy
  const confirm = (): void => { if (blocked) return; if (estimate && estimate.high_cny > limit) setDialog(true); else onConfirm(limit) }
  return (
    <div className="work-body" style={{ position: 'relative' }}>
      <div className="work-left">
        <h2 className="work-title">{unescapeMarkdown(resolved.title)}<span className="state tone-ok"><Check size={13} style={{ verticalAlign: -1 }} /> 检查通过</span></h2>
        {runtimeNotice}
        <WarningList warnings={resolved.warnings ?? []} />
        <div className="confirm-cols">
        <div className="facts-box">
          <div className="row"><span className="k">来源</span><span>{resolved.source_type === 'feishu' ? '飞书知识库' : '本地 Markdown'}</span></div>
          <div className="row"><span className="k">正文</span><span>{estimate?.basis.chars ? `约 ${estimate.basis.chars.toLocaleString('zh-CN')} 字` : '—'}</span></div>
          <div className="row"><span className="k">原文图片</span><span>{estimate?.basis.images ?? resolved.image_count} 张{resolved.content_type ? '' : '（开跑后自动判断截图还是配图）'}</span></div>
          <div className="row"><span className="k">需生成插图</span><span>{illus ? `约 ${illus[0]}～${illus[1]} 张（编排完成后才知道准数）` : '—'}</span></div>
          <div className="row"><span className="k">保存到</span><span className="mono" style={{ fontSize: 13 }}>{outputDir}</span></div>
        </div>
        <section className="chapters" aria-label="章节">
          <h3>章节{chapters && chapters.total > chapters.items.length ? `（共 ${chapters.total} 个，只列前 ${chapters.items.length} 个）` : ''}</h3>
          {chapters?.items.map((chapter, index) => <div key={index} className={chapter.level === 1 ? 'h1' : 'h2'}>{chapter.title}</div>)}
          {chapters && !chapters.items.length && <div className="muted">文章里没有一级、二级标题</div>}
          {!chapters && <div className="muted">—</div>}
        </section>
        </div>
      </div>
      <aside className="work-side">
        <span className="side-label">本次出片</span>
        <div className="cost-card">
          <span className="label">大约花费</span>
          <span className="value">{range}</span>
          {images && voice && <span className="sub">其中插图约 {yuan(images.low_cny)}～{Math.round(images.high_cny)}，配音约 {yuan(voice.high_cny)}</span>}
          <div className="limit-line">
            <span>最多不超过</span>
            <label className="field">¥<input className="text-input limit-input" type="number" min={1} value={limit} aria-label="费用上限" onChange={event => setLimit(Number(event.target.value) || defaultMaxCost)} /></label>
          </div>
          <span className="sub">到了上限会自动停下，已经做好的部分保留；提高上限后点"继续"接着做，不会重复扣费。</span>
        </div>
        <span className="side-label" style={{ marginTop: 8 }}>配置</span>
        <div className="kv"><span className="k">密钥</span><span className={keysLine.ok ? 'tone-ok' : 'tone-warn'}>{keysLine.text}</span></div>
        <div className="kv"><span className="k">RunningHub</span><span className={runninghubLine.tone}>{runninghubLine.text}</span></div>
        {missingKeysText && <button className="link missing-keys" onClick={onOpenKeys}>{missingKeysText}</button>}
        <div className="side-actions">
          <button className="btn" onClick={onBack}>返回</button>
          <button className="btn btn-primary btn-block" disabled={!estimate || blocked} onClick={confirm}>确认并开始出片</button>
        </div>
      </aside>
      {dialog && estimate && (
        <div className="overlay">
          <div className="dialog" role="dialog" aria-label="可能超过费用上限">
            <h2>这篇文章比较长，可能超过你的费用上限</h2>
            <p>大约花费 <b>{range}</b>，你设的上限是 <b>¥{limit}</b>。<br />照样开始也可以：到 ¥{limit} 会自动停下，已做的部分保留，提高上限后点"继续"接着做。</p>
            <div className="acts">
              <label className="raise">把上限改成 ¥<input className="text-input limit-input" type="number" min={1} value={raiseTo} aria-label="调整后的上限" onChange={event => setRaiseTo(Number(event.target.value) || suggested)} /></label>
              <button className="btn btn-lg" disabled={runtimeBusy} onClick={() => onConfirm(limit)}>照样开始（上限 ¥{limit}）</button>
              <button className="btn btn-primary btn-lg" disabled={runtimeBusy} onClick={() => onConfirm(raiseTo)}>上限提到 ¥{raiseTo} 再开始</button>
            </div>
            <button className="link" style={{ alignSelf: 'flex-start', fontSize: 12.5 }} onClick={() => setDialog(false)}>先不开始，回去看看</button>
          </div>
        </div>
      )}
    </div>
  )
}
