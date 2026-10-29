import type { TaskStatus } from '../../shared/types'
import { STATUS_LABELS } from '../status'

export function StatusBadge({ status }: { status: TaskStatus }): React.JSX.Element {
  return <span className={`status-badge status-${status}`}>{STATUS_LABELS[status]}</span>
}
