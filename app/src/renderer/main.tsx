import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { AppV1 } from './v1/AppV1'
import './v1/v1.css'

// 1.0 界面（APP1 重做）。旧界面 App.tsx 保留在仓库里、不再挂载，它的测试照常运行。
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppV1 />
  </StrictMode>
)
