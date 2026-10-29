// 底部流程条：按引擎实际步骤（pipeline/engine/spec.py STEP_ORDER_*）。中文名参考原型。
export interface StepDef { id: string; name: string }

export const THEORY_STEPS: StepDef[] = [
  { id: 'S0', name: '判断类型' }, { id: 'S1', name: '口播改写' }, { id: 'S2', name: '分组' }, { id: 'S3', name: '配音' },
  { id: 'S4', name: '对齐' }, { id: 'S5', name: '图片定位' }, { id: 'S7', name: '编排画面' }, { id: 'S8', name: '出图' },
  { id: 'S9', name: '场景搭建' }, { id: 'S10', name: '渲染成片' }
]

export const SCREENSHOT_STEPS: StepDef[] = [
  { id: 'S0', name: '判断类型' }, { id: 'S1', name: '口播改写' }, { id: 'S2', name: '分组' }, { id: 'S3', name: '配音' },
  { id: 'S4', name: '对齐' }, { id: 'S5', name: '图片定位' }, { id: 'S6', name: '截图分析' }, { id: 'S7', name: '编排画面' },
  { id: 'S8', name: '出图' }, { id: 'S9', name: '场景搭建' }, { id: 'S10', name: '渲染成片' }
]

export type ContentType = 'theory' | 'screenshot'

export function stepsFor(type: ContentType | null | undefined): StepDef[] {
  return type === 'screenshot' ? SCREENSHOT_STEPS : THEORY_STEPS
}

export function stepName(id: string | null | undefined, type?: ContentType | null): string | null {
  if (!id) return null
  return (SCREENSHOT_STEPS.find(step => step.id === id) ?? stepsFor(type).find(step => step.id === id))?.name ?? id
}
