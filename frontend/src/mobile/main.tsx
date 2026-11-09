import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { MobileApp } from './MobileApp'
import { registerServiceWorker } from './pwa'
import './mobile.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename="/m">
      <MobileApp />
    </BrowserRouter>
  </React.StrictMode>,
)

// 放在渲染之后：Service Worker 注册不阻塞首屏。
registerServiceWorker()
