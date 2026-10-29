import { useState } from 'react'
import type { KeyCheckOutcome } from '../../shared/v1/adapter'
import { formatBalance, keyCheckDisplay } from '../../shared/v1/key-status'
import { KEY_SERVICES, serviceConfigured, type KeyFieldId, type KeyServiceDef, type KeyStatusMap } from '../../shared/v1/keys'
import type { ExternalLinkId } from '../../shared/v1/links'

// 引导"填写密钥"和设置"密钥"共用。页面只拿得到"已填写"和最后 4 位，拿不到明文。

export interface KeyEditorProps {
  variant: 'onboard' | 'settings'
  status: KeyStatusMap
  check: KeyCheckOutcome | null
  checking: boolean
  onSave(id: KeyFieldId, value: string): Promise<void>
  onCheck(): void
  onSignup(): void
  // APP4：每个密钥行的"去哪里拿"
  onOpenLink?(link: ExternalLinkId): void
}

const timeLabel = (iso: string): string => {
  const date = new Date(iso)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function maskOf(service: KeyServiceDef, status: KeyStatusMap): string | null {
  if (service.fields.length !== 1) return null
  const field = status[service.fields[0]!.id]
  return field?.filled ? (field.last4 ? `••••${field.last4}` : '••••') : null
}

function StatusCell({ service, status, check }: { service: KeyServiceDef; status: KeyStatusMap; check: KeyCheckOutcome | null }): React.JSX.Element {
  const result = check?.results.find(item => item.service === service.id)
  const configured = serviceConfigured(service, status)
  const partly = service.fields.some(field => status[field.id]?.filled)
  let main: React.JSX.Element
  let sub: string | null = null
  if (result) {
    const display = keyCheckDisplay(result.status)
    const balance = display.showBalance ? formatBalance(result.balance) : null
    main = <span className={`tone-${display.tone}`}>{result.status === 'missing' ? <><span className="ring" /> </> : result.status === 'ok' ? <><span className="dot" /> </> : null}{display.label}</span>
    sub = [balance, check ? `检测于 ${timeLabel(check.checkedAt)}` : null].filter(Boolean).join(' · ')
  } else if (configured) {
    main = <span className="muted">已填写，待检测</span>
  } else {
    main = <span className="tone-warn"><span className="ring" /> {partly ? '还差一项' : '未填写'}</span>
    if (service.optionalNote) sub = service.optionalNote
  }
  return (
    <div className="key-status">
      {main}
      {sub && <span className="sub">{sub}</span>}
    </div>
  )
}

function FieldInput({ id, label, showLabel, filled, last4, onSave }: { id: KeyFieldId; label: string; showLabel: boolean; filled: boolean; last4: string | null; onSave(id: KeyFieldId, value: string): Promise<void> }): React.JSX.Element {
  const [value, setValue] = useState('')
  const commit = async (): Promise<void> => {
    if (!value.trim()) return
    const next = value
    setValue('')
    await onSave(id, next)
  }
  return (
    <div className="key-input-line">
      {showLabel && <span className="lab">{label}</span>}
      <input className="text-input" type="password" autoComplete="off" spellCheck={false} aria-label={label}
        placeholder={filled ? `已填写（${last4 ? `••••${last4}` : '••••'}），重新粘贴可更换` : `粘贴${showLabel ? ' ' + label : '密钥'}`}
        value={value} onChange={event => setValue(event.target.value)} onBlur={() => void commit()} onKeyDown={event => { if (event.key === 'Enter') void commit() }} />
    </div>
  )
}

export function KeyEditor(props: KeyEditorProps): React.JSX.Element {
  const { variant, status, check, checking, onSave, onCheck, onSignup, onOpenLink } = props
  const [editing, setEditing] = useState<string | null>(null)
  const checkButton = <button className="btn" onClick={onCheck} disabled={checking}>{checking ? '正在检测…' : '检测全部密钥'}</button>
  return (
    <div className="key-list">
      {variant === 'settings' && (
        <div className="settings-intro"><span>密钥加密保存在这台电脑上，只在出片时交给引擎，不写进日志，也不会完整显示。</span>{checkButton}</div>
      )}
      {KEY_SERVICES.map(service => {
        const configured = serviceConfigured(service, status)
        const mask = maskOf(service, status)
        const showInputs = variant === 'onboard' || editing === service.id
        return (
          <div key={service.id}>
            <div className={variant === 'settings' && !showInputs ? 'key-row compact' : 'key-row'}>
              <div>
                <div className="key-name">{service.name}{variant === 'settings' && mask && <span className="mask">{mask}</span>}</div>
                <div className="key-purpose">{service.purpose}{service.optionalNote ? `（${service.optionalNote}）` : ''}
                  {onOpenLink && <> · <button className="link key-where" onClick={() => onOpenLink(service.keyLink)}>去哪里拿</button></>}</div>
                {service.howNote && <div className="key-how">{service.howNote}</div>}
                {/* APP2：注册入口只留这一个（右侧不再放"去注册"），换行时左对齐 */}
                {service.id === 'runninghub' && <button className="link signup" onClick={onSignup}>没有账号？点这里注册</button>}
              </div>
              {showInputs && (
                <div className="key-inputs">
                  {service.fields.map(field => <FieldInput key={field.id} id={field.id} label={field.label} showLabel={service.fields.length > 1}
                    filled={status[field.id]?.filled ?? false} last4={status[field.id]?.last4 ?? null} onSave={onSave} />)}
                </div>
              )}
              <div className="key-actions" style={{ justifyContent: 'flex-end' }}>
                <StatusCell service={service} status={status} check={check} />
                {variant === 'settings' && (
                  <div className="key-buttons">
                    <button className="btn btn-sm" onClick={() => setEditing(editing === service.id ? null : service.id)}>{editing === service.id ? '完成' : configured ? '更换' : '填写'}</button>
                    {service.fields.some(field => status[field.id]?.filled) && <button className="btn btn-sm" onClick={() => void Promise.all(service.fields.map(field => onSave(field.id, '')))}>清空</button>}
                  </div>
                )}
              </div>
            </div>
          </div>
        )
      })}
      {variant === 'onboard' && <div className="check-line">{checkButton}<span>检测只查账户，不花钱。方舟、豆包配音暂时没法免费检测，会在第一次出片时验证。</span></div>}
      {check?.error && <div className="tone-danger" style={{ fontSize: 12.5 }}>{check.error}</div>}
    </div>
  )
}
