<template>
  <!-- 管理台主体（token 检查已在 admin.html 完成，此处直接进入） -->
  <!-- 布局:顶部一级导航(概览/会话/调用日志/任务中心/插件/各 pack 页),
       下方整块区域给当前页——不再左右分栏浪费宽度 -->
  <div class="shell">
    <header class="shell-header">
      <div class="shell-brand">
        <div class="brand-mark">AI</div>
        <div class="brand-text">
          <div class="shell-title">LLM Form Modeler <span class="ver">管理端</span></div>
        </div>
      </div>
      <!-- 一级导航(顶部) -->
      <nav class="top-nav">
        <button v-for="item in navItems" :key="item.key"
                :class="['nav-item', { active: tab === item.key }]"
                @click="tab = item.key">
          <component :is="item.icon" class="nav-icon" />
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <div class="shell-actions">
        <a-badge status="processing" text="服务运行中" />
        <a-button size="small" ghost @click="doLogout">退出</a-button>
      </div>
    </header>

    <main class="shell-main">
      <div class="page-card">
        <OverviewTab v-if="tab === 'overview'" />
        <ConversationsTab v-else-if="tab === 'conversations'" />
        <CallLogsTab v-else-if="tab === 'calllogs'" />
        <TasksTab v-else-if="tab === 'tasks'" />
        <PacksTab v-else-if="tab === 'packs'" @open-page="openPackPage" @refresh-pages="loadPackPages" />
        <!-- pack 自定义管理页(v-if 切换:离开即卸载, 图谱类重资源页不留后台实例) -->
        <template v-else-if="packPageMap[tab]">
          <component :is="packPageMap[tab]" />
        </template>
      </div>
      <footer class="shell-footer">LLM Form Modeler Admin · v0.4.0</footer>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, provide, ref } from 'vue'
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

// 一级导航:固定五项 + pack 动态页(智能问数/知识库…)
const navItems = computed(() => [
  { key: 'overview', label: '概览', icon: DashboardOutlined },
  { key: 'conversations', label: '会话', icon: MessageOutlined },
  { key: 'calllogs', label: '调用日志', icon: ApiOutlined },
  { key: 'tasks', label: '任务中心', icon: CloudServerOutlined },
  { key: 'packs', label: '插件', icon: AppstoreOutlined },
  ...packPages.value.map((p) => ({
    key: `pack-page:${p.pageKey}`, label: p.title, icon: PartitionOutlined })),
])

// pack 动态管理页
const packPages = ref<{ pageKey: string; title: string; component: Component }[]>([])
// key → component 映射(v-if 分支用)
const packPageMap = computed<Record<string, Component>>(() =>
  Object.fromEntries(packPages.value.map((p) => [`pack-page:${p.pageKey}`, p.component])))

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
  gap: 24px;
  position: sticky;
  top: 0;
  z-index: 100;
}
.shell-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}
.brand-mark {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: linear-gradient(135deg, #3b82f6, #6366f1);
  color: #fff;
  font-size: 17px;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
}
.shell-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2329;
  white-space: nowrap;
}
.ver {
  font-size: 12px;
  color: #86909c;
  font-weight: 400;
  margin-left: 4px;
}

/* 顶部一级导航 */
.top-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: 1;
  min-width: 0;
  overflow-x: auto;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 6px;
  border: none;
  background: transparent;
  color: #4e5969;
  font-size: 13.5px;
  padding: 7px 14px;
  border-radius: 8px;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s;
}
.nav-item:hover {
  color: #3370ff;
  background: #f2f6ff;
}
.nav-item.active {
  color: #3370ff;
  background: #eaf0ff;
  font-weight: 600;
}
.nav-icon {
  font-size: 14px;
}
.shell-actions {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
}
.shell-main {
  max-width: 1400px;
  width: 100%;
  margin: 0 auto;
  padding: 20px 24px;
}
.page-card {
  background: #fff;
  border-radius: 12px;
  border: 1px solid #e5e6eb;
  padding: 20px 24px;
  min-height: calc(100vh - 160px);
}
.shell-footer {
  text-align: center;
  color: #c9cdd4;
  font-size: 12px;
  padding: 16px 0 8px;
}
</style>