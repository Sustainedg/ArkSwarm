import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.ARKSWARM_BACKEND ?? 'http://127.0.0.1:8000'
const entry = (relative: string) => fileURLToPath(new URL(relative, import.meta.url))

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
        // SSE needs buffering disabled
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache'
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      // 多入口：桌面端 `/` 与移动端 `/m/` 是两条物理独立的链接，各自有 HTML 入口。
      input: { main: entry('index.html'), mobile: entry('m/index.html') },
    },
  },
})
