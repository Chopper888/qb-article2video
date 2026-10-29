import { Sparkles } from 'lucide-react'

export function EmptyState({
  title,
  description,
  action
}: {
  title: string
  description: string
  action?: React.ReactNode
}): React.JSX.Element {
  return (
    <div className="empty-state">
      <span className="empty-icon"><Sparkles size={24} /></span>
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  )
}
