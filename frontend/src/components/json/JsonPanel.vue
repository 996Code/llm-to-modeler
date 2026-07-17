<template>
  <div class="json-panel">
    <div class="panel-header">
      <div class="header-left">
        <CodeOutlined class="header-icon" />
        <span class="title">配置 JSON</span>
        <a-tag v-if="config" color="processing" class="field-count">
          {{ config.formFieldConfigVos?.length || 0 }} 字段
        </a-tag>
      </div>
      <div class="actions">
        <button class="icon-btn" @click="copy" :disabled="!config" title="复制">
          <CopyOutlined />
        </button>
        <button class="icon-btn" @click="download" :disabled="!config" title="下载">
          <DownloadOutlined />
        </button>
        <button
          v-if="store.isEmbedded"
          class="icon-btn primary"
          @click="applyToParent"
          :disabled="!config"
          title="应用到主系统"
        >
          <CheckOutlined />
        </button>
      </div>
    </div>
    <div class="editor-container">
      <div v-if="!config" class="empty">
        <div class="empty-illustration">
          <FileTextOutlined />
        </div>
        <p class="empty-title">暂无配置</p>
        <p class="empty-desc">生成的表单配置将显示在这里</p>
      </div>
      <pre v-else class="json-view" v-html="highlightedJson"></pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { message } from 'ant-design-vue'
import { CopyOutlined, DownloadOutlined, FileTextOutlined, CodeOutlined, CheckOutlined } from '@ant-design/icons-vue'
import { useConversationStore } from '../../stores/conversation'

const store = useConversationStore()

const config = computed(() => store.currentConfig)

const formattedJson = computed(() =>
  config.value ? JSON.stringify(config.value, null, 2) : '',
)

// Minimal JSON syntax highlighter (escape → wrap keys/strings/numbers/bools)
const highlightedJson = computed(() => {
  if (!formattedJson.value) return ''
  const escaped = formattedJson.value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return escaped.replace(
    /("(?:\\.|[^"\\])*"\s*:)|("(?:\\.|[^"\\])*")|(\b-?\d+\.?\d*\b)|(\btrue\b|\bfalse\b|\bnull\b)/g,
    (match, key, str, num, bool) => {
      if (key) return `<span class="j-key">${key.slice(0, -1).replace(/:$/, '')}</span>:`
      if (str) return `<span class="j-str">${str}</span>`
      if (num) return `<span class="j-num">${num}</span>`
      if (bool) return `<span class="j-bool">${bool}</span>`
      return match
    },
  )
})

async function copy() {
  if (!config.value) return
  await navigator.clipboard.writeText(JSON.stringify(config.value, null, 2))
  message.success('已复制到剪贴板')
}

function download() {
  if (!config.value) return
  const blob = new Blob([JSON.stringify(config.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${config.value.formCode || 'form-config'}-${Date.now()}.json`
  a.click()
  URL.revokeObjectURL(url)
}

function applyToParent() {
  if (!config.value) return
  window.parent.postMessage(
    { type: 'MODELER_CONFIG_APPLY', payload: { config: config.value } },
    '*',
  )
  message.success('已发送到主系统')
}
</script>

<style scoped>
.json-panel { display: flex; flex-direction: column; height: 100%; background: var(--bg-container); }

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-color-light);
}
.header-left { display: flex; align-items: center; gap: 8px; }
.header-icon { color: var(--color-primary); font-size: 15px; }
.title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.field-count {
  margin-left: 4px !important;
  border: none !important;
  background: var(--color-primary-light) !important;
  color: var(--color-primary) !important;
  font-size: 11px;
}
.actions { display: flex; gap: 4px; }
.icon-btn {
  width: 30px; height: 30px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color-light);
  background: var(--bg-container);
  color: var(--text-regular);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  transition: all 0.2s;
}
.icon-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-bg);
}
.icon-btn.primary { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }
.icon-btn.primary:hover:not(:disabled) { background: var(--color-primary-hover); border-color: var(--color-primary-hover); }
.icon-btn:disabled { opacity: 0.4; cursor: not-allowed; }

.editor-container { flex: 1; overflow: auto; }
.empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 20px;
}
.empty-illustration {
  width: 64px; height: 64px;
  border-radius: 50%;
  background: var(--bg-hover);
  color: var(--text-placeholder);
  display: flex; align-items: center; justify-content: center;
  font-size: 28px;
  margin-bottom: 16px;
}
.empty-title { font-size: 14px; color: var(--text-secondary); margin-bottom: 4px; }
.empty-desc { font-size: 12px; color: var(--text-placeholder); }

.json-view {
  padding: 16px;
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-regular);
  white-space: pre-wrap;
  word-break: break-all;
}
.json-view :deep(.j-key) { color: #1f6feb; }
.json-view :deep(.j-str) { color: #00a870; }
.json-view :deep(.j-num) { color: #d4a72c; }
.json-view :deep(.j-bool) { color: #f54a45; }
</style>
