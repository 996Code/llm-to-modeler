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
        <!-- 服务运行中徽标 hover 弹「系统配置」面板:平台级显示设置
             (独立/嵌入对话页共读;配置入口只在管理界面,对话界面不支持配置) -->
        <a-popover trigger="hover" placement="bottomRight" overlay-class-name="sys-config-popover">
          <template #title>系统配置</template>
          <template #content>
            <div class="sys-config-panel">
              <!-- 开关 1:对话时的链路/路径/思考信息 -->
              <div class="config-row">
                <div class="config-text">
                  <div class="config-label">显示链路与思考信息</div>
                  <div class="config-desc">关闭后对话仅显示「正在处理…」与最终结果(独立/嵌入对话页生效)</div>
                </div>
                <a-switch size="small" :checked="displaySettings.showPipeline"
                          @change="(v: boolean | string | number) => saveDisplay('showPipeline', !!v)" />
              </div>
              <!-- 开关 2:对话界面的检索结果 -->
              <div class="config-row">
                <div class="config-text">
                  <div class="config-label">显示检索结果</div>
                  <div class="config-desc">关闭后对话中不显示知识图谱检索结果卡</div>
                </div>
                <a-switch size="small" :checked="displaySettings.showRetrieval"
                          @change="(v: boolean | string | number) => saveDisplay('showRetrieval', !!v)" />
              </div>
            </div>
          </template>
          <a-badge status="processing" text="服务运行中" />
        </a-popover>
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
        <!-- pack 自定义管理页(v-if 切换:离开即卸载, 图谱类重资源页不留后台实例);
             知识库-测试也走此通道(loadPackPages 动态追加,跟随插件启停) -->
        <template v-else-if="packPageMap[tab]">
          <component :is="packPageMap[tab]" />
        </template>
      </div>
      <footer class="shell-footer">LLM Form Modeler Admin · v0.4.0</footer>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, provide, ref, shallowRef } from 'vue'
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
import KbEmbedTest from '../embed-test/EmbedTestApp.vue'
import { hasPackPage, packPageRegistry } from './packPages/registry'
import {
  apiErrorMessage, fetchDisplaySettings, fetchPacks, saveDisplaySettings,
  UnauthorizedError,
} from './api'

const tab = ref('overview')

// 一级导航:固定五项 + pack 动态页(智能问数/知识库…)
const navItems = computed(() => [
  { key: 'overview', label: '概览', icon: DashboardOutlined },
  { key: 'conversations', label: '会话', icon: MessageOutlined },
  { key: 'calllogs', label: '审计 & 调用', icon: ApiOutlined },
  { key: 'tasks', label: '任务中心', icon: CloudServerOutlined },
  { key: 'packs', label: '插件', icon: AppstoreOutlined },
  ...packPages.value.map((p) => ({
    key: `pack-page:${p.pageKey}`, label: p.title, icon: PartitionOutlined })),
])

// pack 动态管理页
// 组件定义不应被 Vue 深度代理；列表始终整批替换，shallowRef 正合适。
const packPages = shallowRef<{ pageKey: string; title: string; component: Component }[]>([])
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
    // 知识库-测试:跟随 knowledge_graph 插件启停(一体)——插件启用且依赖
    // 正常才亮菜单,禁用即消失;紧跟知识库管理页后面
    const kg = data.items.find((p) => p.name === 'knowledge_graph')
    if (kg?.enabled && kg.dependency?.status === 'ok') {
      packPages.value.push({ pageKey: '__kb_embed_test', title: '知识库-测试', component: KbEmbedTest })
    }
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

// ── 平台级显示设置(右上角徽标 hover 面板) ──────────────────────
// 独立/嵌入对话页经用户级端点 GET /api/settings/display 共读这一份配置;
// 默认两开关全开(与后端默认值同口径),挂载后拉服务端真实值。
const displaySettings = ref<{ showPipeline: boolean; showRetrieval: boolean }>({
  showPipeline: true,
  showRetrieval: true,
})

/** 切开关 → PUT 落库(乐观更新,失败回滚并提示)。 */
async function saveDisplay(key: 'showPipeline' | 'showRetrieval', value: boolean) {
  const prev = { ...displaySettings.value }
  displaySettings.value = { ...displaySettings.value, [key]: value }
  try {
    displaySettings.value = await saveDisplaySettings({ [key]: value })
  } catch (e) {
    displaySettings.value = prev
    if (e instanceof UnauthorizedError) { onAuthFail(); return }
    message.error(apiErrorMessage(e))
  }
}

onMounted(async () => {
  loadPackPages()
  try {
    displaySettings.value = await fetchDisplaySettings()
  } catch { /* 拉取失败保持默认值;面板开关仍可切(切时再落库) */ }
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
/* 徽标 hover 目标:扩大热区,让 popover 稳定触发 */
.shell-actions :deep(.ant-badge) {
  cursor: default;
  padding: 2px 4px;
  border-radius: 6px;
  transition: background 0.2s;
}
.shell-actions :deep(.ant-badge:hover) {
  background: #f2f6ff;
}

/* ===== 系统配置面板(popover content) ===== */
.sys-config-panel {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 260px;
}
.config-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 8px 4px;
}
.config-row + .config-row {
  border-top: 1px solid #f0f0f0;
}
.config-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.config-label {
  font-size: 13px;
  font-weight: 500;
  color: #1f2329;
}
.config-desc {
  font-size: 12px;
  color: #86909c;
  line-height: 1.4;
}
.shell-main {
  max-width: 1760px;   /* 宽屏自适应:1400 在 2K+ 显示器两侧各留 ~580px 空白(用户 10-15 报"间距太大") */
  width: 100%;
  margin: 0 auto;
  padding: 16px 20px;
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
