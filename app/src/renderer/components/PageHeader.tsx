interface PageHeaderProps {
  title: string
  description: string
  action?: React.ReactNode
}

export function PageHeader({ title, description, action }: PageHeaderProps): React.JSX.Element {
  return (
    <header className="page-header">
      <div><h1>{title}</h1><p>{description}</p></div>
      {action}
    </header>
  )
}
