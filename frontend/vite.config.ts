import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  // 统一项目前缀：宿主（mind-designer/网关）通过 /ai-modeler 反代本应用。
  // base 决定资源与页面都挂在 /ai-modeler/ 下——designer dev 代理与生产网关
  // 反代都「不剥前缀」透传，dev 与生产行为完全一致。
  base: '/ai-modeler/',
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 13080,
    proxy: {
      // 带前缀的 API：经宿主代理访问时（iframe 内相对 origin 是 designer:7114，
      // 请求路径 /ai-modeler/api/... → designer 代理转发到这里 → 再转后端）
      '/ai-modeler/api': {
        target: 'http://127.0.0.1:18080',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/ai-modeler/, ''),
      },
      '/ai-modeler/health': {
        target: 'http://127.0.0.1:18080',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/ai-modeler/, ''),
      },
      // 直连本 dev server（http://localhost:13080/ai-modeler/）时的兼容代理
      '/api': {
        target: 'http://127.0.0.1:18080',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:18080',
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        embed: resolve(__dirname, 'src/embed.ts'),
      },
      output: {
        entryFileNames: (chunkInfo) => {
          if (chunkInfo.name === 'embed') {
            return 'embed.js'
          }
          return 'assets/[name]-[hash].js'
        },
      },
    },
  },
})
