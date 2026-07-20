import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 13080,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18081',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:18081',
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
