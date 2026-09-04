<template>
  <!-- 口令校验中 -->
  <div v-if="authed === null" class="gate">
    <a-spin size="large" />
  </div>

  <!-- 口令门(仅口令模式且未通过时出现;开放模式直接进主体) -->
  <div v-else-if="authed !== true" class="gate">
    <div class="gate-card">
      <div class="brand-mark">AI</div>
      <h2 class="brand-name">LLM Form Modeler</h2>
      <p class="brand-sub">管理端 · 会话审计 / 链路追踪 / 插件治理</p>
      <a-alert v-if="gateError" :message="gateError" type="error" show-icon style="margin-bottom: 16px" />
      <a-form layout="vertical" @submit.prevent>
        <a-form-item label="管理口令(ADMIN_TOKEN)">
          <a-input-password
            v-model:value="tokenInput"
            placeholder="部署时在 .env 配置的 ADMIN_TOKEN"
            @pressEnter="login"
          />
        </a-form-item>
        <a-button type="primary" block :loading="checking" @click="login">进入控制台</a-button>
      </a-form>
    </div>
  </div>

  <!-- 管理台主体 -->
  <div v-else class="shell">
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
        <!-- 退出仅口令模式有意义:开放模式没有"登入"可言,显示它只会让人困惑 -->
        <a-button v-if="authMode === 'token'" size="small" ghost @click="logout">退出</a-button>
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
          <!-- pack 自定义管理页(manifest admin.page 声明 → 注册表解析;异步 chunk) -->
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
// 管理端根组件:开放模式直达 / 口令模式登录门 + 卡片化控制台外壳。
// Tab 结构 = 静态基础 Tab(概览/会话/调用日志/任务中心/插件) + pack 动态 Tab
// (manifest admin.page 声明 → packPages/registry 解析,未注册的 key 优雅降级)。
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
import { apiErrorMessage, fetchPacks, fetchStats, getAdminToken, setAdminToken, UnauthorizedError } from './api'

// null = 校验中;true = 已通过;false = 未登录/口令失效
const authed = ref<boolean | null>(null)
const authMode = ref<'open' | 'token' | null>(null)
const tokenInput = ref('')
const gateError = ref('')
const checking = ref(false)
const tab = ref('overview')

// pack 动态管理页:pageKey → { pageKey, title, component }(仅已启用且依赖正常的)
const packPages = ref<{ pageKey: string; title: string; component: Component }[]>([])

async function loadPackPages() {
  try {
    const data = await fetchPacks()
    packPages.value = data.items
      .map((p) => ({ p, key: p.adminPage || '' }))
      .filter(({ p, key }) => p.enabled && key && p.dependency?.status === 'ok' && hasPackPage(key))
      .map(({ p, key }) => ({ pageKey: key, title: p.adminTitle || p.name, component: packPageRegistry[key] }))
    // 当前停留在已被移除的动态 Tab 上 → 回落插件 Tab
    if (tab.value.startsWith('pack-page:') && !packPages.value.some((p) => `pack-page:${p.pageKey}` === tab.value)) {
      tab.value = 'packs'
    }
  } catch { /* 拉取失败保持现状;插件 Tab 内的开关操作会再次触发刷新 */ }
}

function openPackPage(pageKey: string) {
  if (hasPackPage(pageKey)) tab.value = `pack-page:${pageKey}`
  else message.info('该插件的管理页组件尚未注册(前端未实现)')
}

// 子 Tab 捕获 401 时回调:清口令回登录页;统一错误包装供各 Tab 复用
function onAuthFail() {
  setAdminToken('')
  authed.value = false
  gateError.value = '管理口令无效或已过期,请重新输入'
}
provide('onAuthFail', onAuthFail)
provide('loadSafely', async (fn: () => Promise<void>) => {
  try {
    await fn()
  } catch (e) {
    if (e instanceof UnauthorizedError) {
      onAuthFail()
      return
    }
    message.error(apiErrorMessage(e))
  }
})

async function login() {
  if (!tokenInput.value.trim()) {
    gateError.value = '请输入管理口令'
    return
  }
  checking.value = true
  gateError.value = ''
  setAdminToken(tokenInput.value.trim())
  try {
    const s = await fetchStats()
    authMode.value = s.authMode ?? null
    authed.value = true
    tokenInput.value = ''
  } catch (e) {
    setAdminToken('')
    if (e instanceof UnauthorizedError) gateError.value = '口令无效,请检查 ADMIN_TOKEN 配置'
    else gateError.value = apiErrorMessage(e)
  } finally {
    checking.value = false
  }
}

function logout() {
  setAdminToken('')
  authed.value = false
}

onMounted(async () => {
  // 先静默尝试:开放模式(未配 ADMIN_TOKEN)→ 直接进入,全程无口令门;
  // 401 → 口令门;其他错误 → 口令门并显示后端 detail
  try {
    const s = await fetchStats()
    authMode.value = s.authMode ?? null
    authed.value = true
    loadPackPages()  // 登录态就绪后再拉动态 Tab(不阻塞首屏)
  } catch (e) {
    authed.value = false
    if (!(e instanceof UnauthorizedError)) gateError.value = apiErrorMessage(e)
  }
})
</script>

<style scoped>
.gate {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #f0f5ff 0%, #f5f7fa 45%, #eef2f7 100%);
}
.gate-card {
  width: 400px;
  background: #fff;
  border-radius: 16px;
  padding: 36px 36px 28px;
  box-shadow: 0 12px 40px rgba(30, 60, 120, 0.1);
  text-align: center;
}
.brand-mark {
  width: 48px; height: 48px; border-radius: 12px; margin: 0 auto;
  background: linear-gradient(135deg, #2f54eb, #597ef7);
  color: #fff; font-weight: 700; font-size: 18px;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 4px 12px rgba(47, 84, 235, 0.35);
}
.brand-name { margin: 14px 0 2px; font-size: 18px; color: #1f2937; }
.brand-sub { color: #9ca3af; font-size: 12px; margin-bottom: 22px; text-align: center; }

.shell { min-height: 100vh; background: #f0f2f8; display: flex; flex-direction: column; }
.shell-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 28px; height: 64px;
  background: linear-gradient(90deg, #111c43 0%, #1b2c66 55%, #22397f 100%);
  color: #fff;
  box-shadow: 0 2px 12px rgba(17, 28, 67, 0.28);
  position: sticky; top: 0; z-index: 10;
}
.shell-brand { display: flex; align-items: center; gap: 12px; }
.shell-brand .brand-mark { width: 36px; height: 36px; font-size: 14px; border-radius: 9px; margin: 0; }
.shell-title { font-size: 16px; font-weight: 600; letter-spacing: 0.3px; }
.shell-title .ver {
  font-size: 11px; font-weight: 400; color: #adc6ff; margin-left: 6px;
  border: 1px solid rgba(173, 198, 255, 0.4); border-radius: 4px; padding: 1px 6px;
}
.shell-sub { font-size: 11px; color: rgba(255, 255, 255, 0.55); margin-top: 2px; }
.shell-actions { display: flex; align-items: center; gap: 14px; color: rgba(255,255,255,0.85); }
.shell-actions :deep(.ant-badge-status-text) { color: rgba(255,255,255,0.75); font-size: 12px; }
.shell-main { flex: 1; padding: 20px 28px 8px; max-width: 1440px; width: 100%; margin: 0 auto; }
.tabbar-card {
  background: #fff; border-radius: 14px; padding: 4px 20px 20px;
  box-shadow: 0 2px 10px rgba(30, 41, 82, 0.06); min-height: calc(100vh - 130px);
}
.shell-footer { text-align: center; color: #b6bcc9; font-size: 11px; padding: 14px 0; }
</style>
