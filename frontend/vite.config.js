import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Backend (hook server) runs on :8000. Proxy API + SSE through the dev server
// so the frontend can use same-origin relative URLs.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/sessions': 'http://localhost:8000',
      '/transcript': 'http://localhost:8000',
      '/events': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      // PTY terminal bridge — needs WebSocket upgrade.
      '/terminal': {
        target: 'http://localhost:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
