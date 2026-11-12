import { bytesText, FAIL_TEXT, installButton, updateBarKind, type UpdateState } from '../../shared/v1/auto-update'

// R121M5：APP 内自动更新的界面（顶部提示条 + 小窗口 + "关于"页里的一行）。
// 三处都只按 Main 推过来的 UpdateState 画；按钮只发"立即更新 / 重启安装 / 以后再说 / 跳过 / 重试 / 打开手动下载地址"，
// 具体下载地址、校验、安装都在 Main。

export interface UpdateActions {
  onDownload(): void
  onInstall(): void
  onLater(): void
  onSkip(): void
  onRetry(): void
  onOpenLink(which: 'github' | 'quark'): void
  onShowDetails(): void
}

function ManualLinks({ onOpenLink }: Pick<UpdateActions, 'onOpenLink'>): React.JSX.Element {
  return (
    <>
      <button className="btn btn-sm" onClick={() => onOpenLink('quark')}>夸克网盘下载</button>
      <button className="btn btn-sm" onClick={() => onOpenLink('github')}>GitHub 下载</button>
    </>
  )
}

function Progress({ state }: { state: UpdateState }): React.JSX.Element {
  return (
    <div className="update-progress" role="progressbar" aria-label="下载进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={state.percent}>
      <span style={{ width: `${state.percent}%` }} />
    </div>
  )
}

function InstallButton({ state, onInstall, primary = true }: { state: UpdateState; onInstall(): void; primary?: boolean }): React.JSX.Element {
  const button = installButton(state)
  // 出片中置灰：不用主色，免得看起来还能点
  return <button className={primary && !button.disabled ? 'btn btn-sm btn-primary' : 'btn btn-sm'} disabled={button.disabled} onClick={onInstall}>{button.text}</button>
}

// 顶部提示条（不是弹窗；不挡操作）
export function UpdateBar({ state, actions }: { state: UpdateState | null; actions: UpdateActions }): React.JSX.Element | null {
  const kind = updateBarKind(state)
  if (!state || !kind) return null
  if (kind === 'available') {
    return (
      <div className="update-bar" role="status" aria-label="软件更新">
        <span className="t">发现新版本 {state.version}</span>
        <button className="link" onClick={actions.onShowDetails}>查看更新内容</button>
        <span className="grow" />
        <button className="btn btn-sm btn-primary" onClick={actions.onDownload}>立即更新</button>
        <button className="btn btn-sm" onClick={actions.onLater}>以后再说</button>
      </div>
    )
  }
  if (kind === 'downloading') {
    return (
      <div className="update-bar" role="status" aria-label="软件更新">
        <span className="t">正在下载新版本 {state.version}</span>
        <span className="pct">{state.percent}%</span>
        <Progress state={state} />
        <button className="link" onClick={actions.onShowDetails}>查看</button>
      </div>
    )
  }
  if (kind === 'downloaded') {
    return (
      <div className="update-bar ok" role="status" aria-label="软件更新">
        <span className="t">新版本 {state.version} 已下载</span>
        <span className="d">{state.busy ? '正在出片，不会打断；出片结束后可以安装' : '关闭软件时会自动安装'}</span>
        <span className="grow" />
        <InstallButton state={state} onInstall={actions.onInstall} />
      </div>
    )
  }
  return (
    <div className="update-bar danger" role="alert" aria-label="软件更新">
      <span className="t">自动更新失败</span>
      <span className="d">{state.failStage ? FAIL_TEXT[state.failStage] : ''}，可以手动下载安装（设置和文章都会保留）</span>
      <span className="grow" />
      <ManualLinks onOpenLink={actions.onOpenLink} />
      <button className="btn btn-sm" onClick={actions.onRetry}>重试</button>
      <button className="link update-close" aria-label="关闭提示" onClick={actions.onLater}>×</button>
    </div>
  )
}

// 小窗口：更新内容 + 下载进度。可以关，关了下载在后台继续，提示条显示进度
export function UpdateDialog({ state, actions, onClose }: { state: UpdateState; actions: UpdateActions; onClose(): void }): React.JSX.Element {
  const title = state.phase === 'downloading' ? `正在下载新版本 ${state.version ?? ''}`
    : state.phase === 'downloaded' ? `新版本 ${state.version ?? ''} 已下载`
      : state.phase === 'failed' ? '自动更新失败'
        : `发现新版本 ${state.version ?? ''}`
  return (
    <div className="overlay update-overlay" onClick={onClose}>
      <div className="dialog update-dialog" role="dialog" aria-label="软件更新" onClick={event => event.stopPropagation()}>
        <div className="update-head">
          <h2>{title}</h2>
          <button className="link update-close" aria-label="关闭" onClick={onClose}>×</button>
        </div>
        <p>当前版本 {state.currentVersion}{state.version && state.phase !== 'failed' ? ` → 新版本 ${state.version}` : ''}。更新会保留你的设置、密钥、工作文件夹和已经出好的片子。</p>
        {state.phase !== 'failed' && (
          <div className="update-notes-box">
            <div className="h">更新内容</div>
            {state.notes.length > 0
              ? <ul className="update-notes">{state.notes.map((line, i) => <li key={i}>{line}</li>)}</ul>
              : <div className="muted">这个版本没有写更新内容。</div>}
          </div>
        )}
        {state.phase === 'downloading' && (
          <div className="update-dl">
            <Progress state={state} />
            <div className="muted">{state.percent}%{state.totalBytes > 0 ? `（${bytesText(state.transferredBytes)} / ${bytesText(state.totalBytes)}）` : ''} · 可以关掉这个窗口，下载会在后台继续</div>
          </div>
        )}
        {state.phase === 'downloaded' && (
          <p>{state.busy ? '现在正在出片，不会打断。出片结束后可以点"重启安装"；' : '可以现在点"重启安装"；'}不点也行，下次关闭软件时会自动安装。</p>
        )}
        {state.phase === 'failed' && (
          <p className="tone-warn">{state.failStage ? FAIL_TEXT[state.failStage] : ''}。可以点"重试"，或者手动下载安装包覆盖安装（设置和文章都会保留）。</p>
        )}
        <div className="acts">
          {state.phase === 'available' && <>
            <button className="btn btn-sm" style={{ marginRight: 'auto' }} onClick={() => { actions.onSkip(); onClose() }}>跳过这个版本</button>
            <button className="btn btn-sm" onClick={() => { actions.onLater(); onClose() }}>以后再说</button>
            <button className="btn btn-sm btn-primary" onClick={actions.onDownload}>立即更新</button>
          </>}
          {state.phase === 'downloading' && <button className="btn btn-sm" onClick={onClose}>关闭窗口</button>}
          {state.phase === 'downloaded' && <>
            <button className="btn btn-sm" onClick={onClose}>稍后</button>
            <InstallButton state={state} onInstall={actions.onInstall} />
          </>}
          {state.phase === 'failed' && <>
            <ManualLinks onOpenLink={actions.onOpenLink} />
            <button className="btn btn-sm btn-primary" onClick={actions.onRetry}>重试</button>
          </>}
        </div>
      </div>
    </div>
  )
}

// "关于"页里"检查更新"下面的一行：和提示条走同一个流程
export function UpdateAboutStatus({ state, actions }: { state: UpdateState | null; actions: UpdateActions }): React.JSX.Element | null {
  if (!state) return null
  switch (state.phase) {
    case 'latest': return <div className="tone-ok" role="status">已是最新版本（{state.currentVersion}）</div>
    case 'unsupported': return <div className="muted" role="status">开发模式不检查更新（安装版才有自动更新）</div>
    case 'available': return (
      <div className="update-about" role="status">
        <span>发现新版本 {state.version}{state.skipped ? '（之前选过跳过这个版本）' : ''}</span>
        <button className="link" onClick={actions.onShowDetails}>查看更新内容</button>
        <button className="btn btn-sm btn-primary" onClick={actions.onDownload}>立即更新</button>
      </div>
    )
    case 'downloading': return (
      <div className="update-about" role="status"><span>正在下载新版本 {state.version}：{state.percent}%</span><button className="link" onClick={actions.onShowDetails}>查看</button></div>
    )
    case 'downloaded': return (
      <div className="update-about" role="status">
        <span>新版本 {state.version} 已下载，关闭软件时会自动安装</span>
        <InstallButton state={state} onInstall={actions.onInstall} />
      </div>
    )
    case 'failed': return (
      <div className="update-about" role="status">
        <span className="tone-warn">{state.failStage === 'check' ? '暂时连不上更新服务器，可以稍后再试，或者手动下载：' : `自动更新失败：${state.failStage ? FAIL_TEXT[state.failStage] : ''}。可以手动下载：`}</span>
        <ManualLinks onOpenLink={actions.onOpenLink} />
      </div>
    )
    default: return null
  }
}
