import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Dev only: where the FastAPI backend runs. The browser always talks to its
  // own origin (/api, /ws) exactly like in production; Vite forwards both.
  const backend = loadEnv(mode, process.cwd(), '').VITE_BACKEND_URL || 'http://localhost:8000'
  return {
    plugins: [react()],
    server: {
      proxy: {
        '/api': { target: backend, changeOrigin: true },
        '/ws': { target: backend, ws: true, changeOrigin: true },
      },
    },
  }
})
