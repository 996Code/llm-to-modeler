<template>
  <!-- 管理台主体（token 检查已在 admin.html 完成，此处直接进入） -->
  <div class="shell">
    <header class="shell-header">
      <div class="shell-brand">
        <div class="brand-mark">AI</div>
        <div>
          <div class="shell-title">LLM Form Modeler <span class="ver">管理端</span></div>
          <div class="shell-sub">会话审计 · 链路追踪 · 调用观测 · 插件治理</div>
        </div>
      </div>
      <div class="shell-actions">
        <a-badge status="processing" text="服务运行中" />
        <a-button size="small" ghost @click="doLogout">退出</a-button>
      </div>
    </header>

    <main class="shell-main">
      <div class="tabbar-card">
        <a-tabs v-model:activeKey="tab" size="large">
          <a-tab-pane key="overview">
            <template #tab><DashboardOutlined /> 概览</template>
            <OverviewTab />
          </a-tab-pane>
          <a-tab-pane key="conversations">
            <template #tab><MessageOutlined /> 会话</template>
            <ConversationsTab />
          </a-tab-pane>
          <a-tab-pane key="calllogs">
            <template #tab><ApiOutlined /> 调用日志</template>
            <CallLogsTab />
          </a-tab-pane>
          <a-tab-pane key="tasks">
            <template #tab><CloudServerOutlined /> 任务中心</template>
            <TasksTab />
          </a-tab-pane>
          <a-tab-pane key="packs">
            <template #tab><AppstoreOutlined /> 插件</template>
            <PacksTab @open-page="openPackPage" @refresh-pages="loadPackPages" />
          </a-tab-pane>
          <!-- pack 自定义管理页 -->
          <a-tab-pane v-for="p in packPages" :key="`pack-page:${p.pageKey}`">
            <template #tab><PartitionOutlined /> {{ p.title }}</template>
            <component :is="p.component" />
          </a-tab-pane>
        </a-tabs>
      </div>
      <footer class="shell-footer">LLM Form Modeler Admin · v0.4.0</footer>
    </main>
  </div>
</template>

<script setup lang="ts">
import { onMounted, provide, ref } from 'vue'
import { message } from 'ant-design-vue'
import type { Component } from 'vue'
import {
  ApiOutlined, AppstoreOutlined, CloudServerOutlined, DashboardOutlined,
  MessageOutlined, PartitionOutlined,
} from '@ant-design/icons-vue'
import OverviewTab from './components/OverviewTab.vue'
import ConversationsTab from './components/ConversationsTab.vue'
import CallLogsTab from './components/CallLogsTab.vue'
import TasksTab from './components/TasksTab.vue'
import PacksTab from './components/PacksTab.vue'
import { hasPackPage, packPageRegistry } from './packPages/registry'
import { apiErrorMessage, fetchPacks, UnauthorizedError } from './api'

const tab = ref('overview')

// pack 动态管理页
const packPages = ref<{ pageKey: string; title: string; component: Component }[]>([])

async function loadPackPages() {
  try {
    const data = await fetchPacks()
    packPages.value = data.items
      .map((p) => ({ p, key: p.adminPage || '' }))
      .filter(({ p, key }) => p.enabled && key && p.dependency?.status === 'ok' && hasPackPage(key))
      .map(({ p, key }) => ({ pageKey: key, title: p.adminTitle || p.name, component: packPageRegistry[key] }))
    if (tab.value.startsWith('pack-page:') && !packPages.value.some((p) => `pack-page:${p.pageKey}` === tab.value)) {
      tab.value = 'packs'
    }
  } catch { /* 保持现状 */ }
}

function openPackPage(pageKey: string) {
  if (hasPackPage(pageKey)) tab.value = `pack-page:${pageKey}`
  else message.info('该插件的管理页组件尚未注册(前端未实现)')
}

// 401 回调：清 token 跳 auth.html
function onAuthFail() {
  localStorage.removeItem('auth_token')
  window.location.href = '/ai-modeler/auth.html?page=admin'
}
provide('onAuthFail', onAuthFail)
provide('loadSafely', async (fn: () => Promise<void>) => {
  try {
    await fn()
  } catch (e) {
    if (e instanceof UnauthorizedError) { onAuthFail(); return }
    message.error(apiErrorMessage(e))
  }
})

function doLogout() {
  localStorage.removeItem('auth_token')
  window.location.href = '/ai-modeler/auth.html?page=admin'
}

onMounted(async () => {
  loadPackPages()
})
</script>

<style scoped>
.shell {
  min-height: 100vh;
  background: #f0f2f5;
}
.shell-header {
  background: #fff;
  border-bottom: 1px solid #e5e6eb;
  padding: 0 24px;
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.shell-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-mark {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: linear-gradient(135deg, #3b82f6, #6366f1);
  color: #fff;
  font-size: 18px;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
}
.shell-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2329;
}
.ver {
  font-size: 12px;
  color: #86909c;
  font-weight: 400;
  margin-left: 4px;
}
.shell-sub {
  font-size: 12px;
  color: #86909c;
}
.shell-actions {
  display: flex;
  align-items: center;
  gap: 16px;
}
.shell-main {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
}
.tabbar-card {
  background: #fff;
  border-radius: 12px;
  border: 1px solid #e5e6eb;
  padding: 0 24px 24px;
}
.shell-footer {
  text-align: center;
  padding: 24px 0 8px;
  font-size: 12px;
  color: #c9cdd4;
}
</style>