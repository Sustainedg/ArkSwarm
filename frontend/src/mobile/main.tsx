import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { initTheme } from '../lib/theme'
import { MobileApp } from './MobileApp'
import { AuthGate } from '../components/AuthGate'
import { registerServiceWorker } from './pwa'
import './mobile.css'

// 先把外观（深色 / 浅色 / 跟随系统）落到 <html> 上，再渲染，避免首屏闪一下。
initTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename="/m">
      <AuthGate mobile>
        <MobileApp />
      </AuthGate>
    </BrowserRouter>
  </React.StrictMode>,
)

// 放在渲染之后：Service Worker 注册不阻塞首屏。
registerServiceWorker()
