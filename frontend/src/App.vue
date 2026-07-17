<template>
  <StandaloneLayout v-if="!store.isEmbedded" />
  <EmbeddedLayout v-else />
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useConversationStore } from './stores/conversation'
import { setForwardedHeaders } from './composables/forwardHeaders'
import StandaloneLayout from './layouts/StandaloneLayout.vue'
import EmbeddedLayout from './layouts/EmbeddedLayout.vue'

const store = useConversationStore()

onMounted(() => {
  // 嵌入模式：监听父系统通过 postMessage 传入的 headers
  if (store.isEmbedded) {
    window.addEventListener('message', (e) => {
      if (e.data?.type === 'MODELER_INIT' && e.data.payload?.headers) {
        setForwardedHeaders(e.data.payload.headers)
      }
    })
  } else {
    store.loadConversations()
  }
})
</script>

<style>
/* ===== Design tokens (MaxKB-inspired) ===== */
:root {
  --color-primary: #3370ff;
  --color-primary-hover: #2860e6;
  --color-primary-light: #eaf0ff;
  --color-primary-bg: #f0f4ff;
  --color-success: #00a870;
  --color-danger: #f54a45;
  --color-warning: #ff9e29;

  --text-primary: #1f2329;
  --text-regular: #4e5969;
  --text-secondary: #86909c;
  --text-placeholder: #c9cdd4;

  --border-color: #e5e6eb;
  --border-color-light: #f0f1f3;
  --border-color-lighter: #f7f8fa;

  --bg-page: #f5f6f8;
  --bg-container: #ffffff;
  --bg-hover: #f7f8fa;
  --bg-active: var(--color-primary-light);

  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;

  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
  --shadow-md: 0 2px 8px rgba(0, 0, 0, 0.06);
  --shadow-input: 0 2px 12px rgba(51, 112, 255, 0.08);

  --font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  --font-mono: 'SF Mono', 'Fira Code', 'JetBrains Mono', Menlo, Consolas, monospace;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; font-family: var(--font-family); color: var(--text-primary); }

/* Custom scrollbar */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #d5d7db; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #b8babe; }

/* Override Ant Design primary color to match MaxKB brand */
.ant-btn-primary {
  background: var(--color-primary) !important;
  border-color: var(--color-primary) !important;
}
.ant-btn-primary:not(:disabled):hover {
  background: var(--color-primary-hover) !important;
  border-color: var(--color-primary-hover) !important;
}
.ant-tag {
  border-radius: var(--radius-md) !important;
}
</style>
