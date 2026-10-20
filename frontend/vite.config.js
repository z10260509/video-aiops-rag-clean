import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    proxy: {
      '/video/api': {
        target: process.env.VIDEO_API_PROXY_TARGET || 'http://127.0.0.1:8000',
        rewrite: (path) => path.replace(/^\/video/, ''),
      },
      '/aiops/api': {
        target: process.env.AIOPS_API_PROXY_TARGET || 'http://127.0.0.1:8010',
        rewrite: (path) => path.replace(/^\/aiops/, ''),
      },
    },
  },
})
