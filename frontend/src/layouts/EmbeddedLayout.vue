<template>
  <div class="embedded-layout">
    <div class="embedded-header">
      <div class="header-brand">
        <div class="brand-logo">
          <FormOutlined />
        </div>
        <div class="header-text">
          <span class="title">表单配置助手</span>
          <span class="subtitle">自然语言 → 低码表单配置</span>
        </div>
      </div>
      <a-button type="text" size="small" class="close-btn" @click="closeWindow">
        <CloseOutlined />
      </a-button>
    </div>
    <ChatPanel :embedded="true" />
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { CloseOutlined, FormOutlined } from '@ant-design/icons-vue'
import { useConversationStore } from '../stores/conversation'
import { useEmbedBridge } from '../composables/useEmbedBridge'
import ChatPanel from '../components/chat/ChatPanel.vue'

const store = useConversationStore()
const { closeWindow } = useEmbedBridge()

onMounted(() => {
  if (!store.currentConversation) {
    store.startNewConversation()
  }
})
</script>

<style scoped>
.embedded-layout {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--bg-page);
}
.embedded-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  background: var(--bg-container);
  border-bottom: 1px solid var(--border-color-light);
  box-shadow: var(--shadow-sm);
}
.header-brand { display: flex; align-items: center; gap: 10px; }
.brand-logo {
  width: 32px; height: 32px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--color-primary), #5b8cff);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 16px;
  box-shadow: 0 2px 8px rgba(51, 112, 255, 0.3);
}
.header-text { display: flex; flex-direction: column; line-height: 1.3; }
.title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.subtitle { font-size: 11px; color: var(--text-secondary); }
.close-btn { color: var(--text-secondary); }
.close-btn:hover { color: var(--text-primary); background: var(--bg-hover) !important; }
</style>
