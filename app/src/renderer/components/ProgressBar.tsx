export function ProgressBar({ value }: { value: number }): React.JSX.Element {
  return (
    <div className="progress-row" aria-label={`进度 ${value}%`}>
      <div className="progress-track"><span style={{ width: `${value}%` }} /></div>
      <strong>{value}%</strong>
    </div>
  )
}
