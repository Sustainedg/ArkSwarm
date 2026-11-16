import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { AuthGate } from './components/AuthGate'
import { initTheme } from './lib/theme'
import './index.css'

// 先把外观（深色 / 浅色 / 跟随系统）落到 <html> 上，再渲染，避免首屏闪一下。
initTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthGate>
        <App />
      </AuthGate>
    </BrowserRouter>
  </React.StrictMode>,
)
