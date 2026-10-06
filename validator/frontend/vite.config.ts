import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Dev-only: forwards /api/* to the real FastAPI backend (started
    // separately, e.g. by hand on port 18234 while developing) so `npm run
    // dev` doesn't need its own CORS setup. The production build (served
    // by app.py itself, see ../run.sh) doesn't use this -- same origin
    // there, no proxy needed.
    proxy: {
      '/api': 'http://127.0.0.1:18234',
    },
  },
})
