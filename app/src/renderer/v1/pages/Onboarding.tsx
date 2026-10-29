import { Check } from 'lucide-react'
import type { V1Prefs } from '../../../shared/v1/types'
import { KeyEditor, type KeyEditorProps } from '../KeyEditor'
import { Logo } from '../parts'

const STEPS = ['欢迎', '填写密钥', '工作文件夹']

function Stepper({ at }: { at: number }): React.JSX.Element {
  return (
    <div className="stepper" aria-label="引导步骤">
      {STEPS.map((name, index) => (
        <span key={name} style={{ display: 'contents' }}>
          {index > 0 && <span className="line" />}
          <span className={`s ${index === at ? 'on' : index < at ? 'done' : ''}`}>
            <span className="num">{index < at ? <Check size={13} /> : index + 1}</span>{name}
          </span>
        </span>
      ))}
    </div>
  )
}

export interface OnboardingProps {
  step: number
  onStep(step: number): void
  keys: Omit<KeyEditorProps, 'variant'>
  prefs: V1Prefs
  onChooseFolder(target: 'work' | 'output'): void
  onFinish(): void
}

export function Onboarding({ step, onStep, keys, prefs, onChooseFolder, onFinish }: OnboardingProps): React.JSX.Element {
  return (
    <div className="onboard">
      <div className="onboard-card">
        <Stepper at={step} />
        {step === 0 && (
          <>
            <Logo large />
            <h2>欢迎使用文章成片</h2>
            <p className="p">把一篇飞书文章或本地 Markdown，自动做成带配音、插图、截图标注的讲解视频。<br />开始前需要两分钟做两件事：填好各家服务的密钥，选一个放文件的位置。</p>
            <div className="facts">
              <div className="fact"><span className="a">一篇大约</span><span className="b">¥10～30</span><span className="c">主要是插图费用</span></div>
              <div className="fact"><span className="a">一篇大约</span><span className="b">30～90 分钟</span><span className="c">可以离开，做完提醒你</span></div>
              <div className="fact"><span className="a">费用上限</span><span className="b">默认 ¥{prefs.maxCostCny}</span><span className="c">到了就停，已做的部分不会白花</span></div>
            </div>
            <div className="onboard-foot"><span /><button className="btn btn-primary btn-lg" onClick={() => onStep(1)}>开始设置</button></div>
          </>
        )}
        {step === 1 && (
          <>
            <h2>填写密钥</h2>
            <p className="p">密钥加密保存在这台电脑上，不会上传，也不会完整显示。</p>
            <KeyEditor variant="onboard" {...keys} />
            <div className="onboard-foot"><button className="btn btn-lg" onClick={() => onStep(0)}>上一步</button><button className="btn btn-primary btn-lg" onClick={() => onStep(2)}>下一步</button></div>
          </>
        )}
        {step === 2 && (
          <>
            <h2>工作文件夹</h2>
            <p className="p">选两个位置：一个放每篇文章的中间文件，一个放做好的成片。路径越短越好。</p>
            <div className="folder-row">
              <div><div className="t">工作文件夹</div><div className="key-purpose">每篇文章的中间文件</div></div>
              <span className="path mono" title={prefs.workDir}>{prefs.workDir}</span>
              <button className="btn btn-sm" onClick={() => onChooseFolder('work')}>更改</button>
            </div>
            <div className="folder-row">
              <div><div className="t">成片保存位置</div><div className="key-purpose">做好的视频</div></div>
              <span className="path mono" title={prefs.outputDir}>{prefs.outputDir}</span>
              <button className="btn btn-sm" onClick={() => onChooseFolder('output')}>更改</button>
            </div>
            <p className="p" style={{ fontSize: 12.5 }}>提示：路径越短越好（Windows 对路径长度有限制）。以后可以在"设置 → 工作文件夹与清理"里改。</p>
            <div className="onboard-foot"><button className="btn btn-lg" onClick={() => onStep(1)}>上一步</button><button className="btn btn-primary btn-lg" onClick={onFinish}>完成，开始使用</button></div>
          </>
        )}
      </div>
    </div>
  )
}
