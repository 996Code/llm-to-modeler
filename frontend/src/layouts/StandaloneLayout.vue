<template>
  <a-layout class="standalone-layout">
    <a-layout-sider :width="248" theme="light" class="sider">
      <div class="sider-header">
        <div class="brand">
          <div class="brand-logo">
            <FormOutlined />
          </div>
          <span class="brand-name">智能助手</span>
        </div>
        <a-button type="primary" class="new-btn" @click="store.startNewConversation">
          <template #icon><PlusOutlined /></template>
          新建对话
        </a-button>
      </div>
      <div class="conv-list">
        <div class="conv-list-title">历史对话</div>
        <div
          v-for="conv in store.conversations"
          :key="conv.id"
          class="conv-item"
          :class="{ active: conv.id === store.currentConversation?.id }"
          @click="store.selectConversation(conv.id)"
        >
          <MessageOutlined class="conv-icon" />
          <span class="conv-title">{{ conv.title }}</span>
          <DeleteOutlined class="conv-del" @click.stop="store.removeConversation(conv.id)" />
        </div>
        <div v-if="!store.conversations.length" class="empty-list">
          暂无历史对话
        </div>
      </div>
    </a-layout-sider>

    <div class="main-area">
      <ChatPanel />
    </div>

    <div class="json-area">
      <JsonPanel />
    </div>
  </a-layout>
</template>

<script setup lang="ts">
import { PlusOutlined, MessageOutlined, DeleteOutlined, FormOutlined } from '@ant-design/icons-vue'
import { useConversationStore } from '../stores/conversation'
import ChatPanel from '../components/chat/ChatPanel.vue'
import JsonPanel from '../components/json/JsonPanel.vue'

const store = useConversationStore()
</script>

<style scoped>
.standalone-layout { height: 100vh; flex-direction: row; }

/* ===== Sidebar ===== */
.sider {
  border-right: 1px solid var(--border-color-light);
  display: flex;
  flex-direction: column;
  background: var(--bg-container) !important;
}
.sider-header {
  padding: 16px;
  border-bottom: 1px solid var(--border-color-lighter);
}
.brand { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.brand-logo {
  width: 30px; height: 30px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--color-primary), #5b8cff);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 15px;
  box-shadow: 0 2px 8px rgba(51, 112, 255, 0.3);
}
.brand-name { font-size: 15px; font-weight: 600; color: var(--text-primary); }
.new-btn { width: 100%; border-radius: var(--radius-md) !important; height: 36px; }

.conv-list { flex: 1; overflow-y: auto; padding: 8px; }
.conv-list-title {
  font-size: 12px;
  color: var(--text-placeholder);
  padding: 8px 8px 6px;
  font-weight: 500;
}
.conv-item {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 10px;
  border-radius: var(--radius-md);
  cursor: pointer;
  font-size: 13px;
  color: var(--text-regular);
  margin-bottom: 2px;
  transition: background 0.2s, color 0.2s;
}
.conv-item:hover { background: var(--bg-hover); }
.conv-item.active { background: var(--bg-active); color: var(--color-primary); font-weight: 500; }
.conv-icon { font-size: 14px; flex-shrink: 0; opacity: 0.7; }
.conv-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-del {
  font-size: 12px;
  opacity: 0;
  transition: opacity 0.2s, color 0.2s;
}
.conv-item:hover .conv-del { opacity: 0.5; }
.conv-del:hover { opacity: 1 !important; color: var(--color-danger); }
.empty-list {
  padding: 20px 10px;
  text-align: center;
  color: var(--text-placeholder);
  font-size: 13px;
}

/* ===== Main areas ===== */
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--bg-page);
}
.json-area {
  width: 40%;
  max-width: 560px;
  border-left: 1px solid var(--border-color-light);
  display: flex;
  flex-direction: column;
  background: var(--bg-container);
}

@media (max-width: 1024px) {
  .standalone-layout { flex-direction: column; }
  .json-area { width: 100%; max-width: none; border-left: none; border-top: 1px solid var(--border-color-light); height: 40%; }
  .sider { display: none; }
}
</style>
