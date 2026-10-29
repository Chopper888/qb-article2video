// key_check.status → 显示（约定 v5.2 第 2.7 节、任务书第四节第 3 条）。

export const KEY_CHECK_STATUSES = ['ok', 'missing', 'key_invalid', 'balance_insufficient', 'network', 'unknown'] as const
export type KeyCheckStatus = typeof KEY_CHECK_STATUSES[number]

export interface KeyCheckDisplay {
  label: string
  tone: 'ok' | 'warn' | 'danger' | 'muted'
  // 是否显示余额（只有 ok 且引擎带了余额时）
  showBalance: boolean
}

const DISPLAY: Record<KeyCheckStatus, KeyCheckDisplay> = {
  ok: { label: '可用', tone: 'ok', showBalance: true },
  missing: { label: '未填写', tone: 'warn', showBalance: false },
  key_invalid: { label: '密钥错误', tone: 'danger', showBalance: false },
  balance_insufficient: { label: '余额不足', tone: 'danger', showBalance: false },
  network: { label: '网络问题，稍后再试', tone: 'warn', showBalance: false },
  unknown: { label: '已填写，首次出片时验证', tone: 'muted', showBalance: false }
}

export function keyCheckDisplay(status: string): KeyCheckDisplay {
  return (DISPLAY as Record<string, KeyCheckDisplay>)[status] ?? { label: '检测结果未知', tone: 'muted', showBalance: false }
}

export interface KeyBalance { currency: string | null; total: string | number | null }

export function formatBalance(balance: KeyBalance[] | undefined): string | null {
  const first = balance?.find(item => item.total !== null && item.total !== undefined)
  if (!first) return null
  const currency = first.currency === 'CNY' ? '¥' : first.currency === 'USD' ? '$' : ''
  const unit = currency ? '' : ` ${first.currency ?? ''}`.trimEnd()
  return `余额 ${currency}${first.total}${unit}`
}
