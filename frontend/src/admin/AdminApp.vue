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
        <!-- 内网系统不放退出入口(2026-11-03 裁决);服务状态=绿点呼吸胶囊 -->
        <span class="svc-pill"><i></i>服务运行中</span>
      </div>
    </header>

    <main class="shell-main">
      <div class="page-card">
        <!-- 概览:只有总览一屏(其余视图归「活动记录」) -->
        <OverviewTab v-if="tab === 'overview'" />
        <PacksTab v-else-if="tab === 'packs'" @open-page="openPackPage" @refresh-pages="loadPackPages" />
        <!-- 活动记录:会话/任务/调用/统计/审计 五视图一层 tab(页面内部
             视图已提升到组级,不再 tab 套 tab) -->
        <a-tabs v-else-if="tab === 'activity'" v-model:activeKey="activityTab" class="flat-tabs">
          <a-tab-pane key="conversations">
            <template #tab><MessageOutlined /> 会话</template>
            <ConversationsTab v-if="activityTab === 'conversations'" />
          </a-tab-pane>
          <a-tab-pane key="tasks">
            <template #tab><CloudServerOutlined /> 任务</template>
            <TasksTab v-if="activityTab === 'tasks'" />
          </a-tab-pane>
          <a-tab-pane key="detail">
            <template #tab><ApiOutlined /> 调用明细</template>
            <CallLogsTab view-mode="detail" />
          </a-tab-pane>
          <a-tab-pane key="stats">
            <template #tab><BarChartOutlined /> 环节统计</template>
            <CallLogsTab view-mode="stats" />
          </a-tab-pane>
          <a-tab-pane key="audit">
            <template #tab><AuditOutlined /> 审计日志</template>
            <CallLogsTab view-mode="audit" />
          </a-tab-pane>
          <template #rightExtra>
            <a-popover :key="activityTab" placement="bottomRight" trigger="click">
              <template #content>
                <div class="help-pop" v-html="activityHelpHtml"></div>
              </template>
              <a-button type="text" size="small" :aria-label="activityHelpTitle" class="help-link">
                <template #icon><QuestionCircleOutlined /></template>说明
              </a-button>
            </a-popover>
          </template>
        </a-tabs>
        <!-- pack 自定义管理页(v-if 切换:离开即卸载, 图谱类重资源页不留后台实例);
             知识库-测试/智能客服-测试也走此通道(loadPackPages 动态追加,跟随插件启停) -->
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
import {ApiOutlined, AppstoreOutlined, AuditOutlined, BarChartOutlined, CloudServerOutlined, DashboardOutlined, MessageOutlined, PartitionOutlined, QuestionCircleOutlined} from '@ant-design/icons-vue'
import { rules } from './helpHtml'
import OverviewTab from './components/OverviewTab.vue'
import ConversationsTab from './components/ConversationsTab.vue'
import CallLogsTab from './components/CallLogsTab.vue'
import TasksTab from './components/TasksTab.vue'
import PacksTab from './components/PacksTab.vue'
import KbEmbedTest from '../embed-test/EmbedTestApp.vue'
import MetrologyTestApp from '../embed-test/MetrologyTestApp.vue'
import { hasPackPage, packPageRegistry } from './packPages/registry'
import {
  apiErrorMessage, fetchPacks,
  UnauthorizedError,
} from './api'

const tab = ref('overview')
// 活动记录 组内一层 tab(会话/任务/调用三视图;调用视图是 CallLogsTab
// 内部 viewMode 提升而来,不出现 tab 套 tab)
const activityTab = ref<'conversations' | 'tasks' | 'detail' | 'stats' | 'audit'>('conversations')

// ── 记录页说明(与其他页同标准:编号条目,配 helpHtml.ts) ──
const activityHelpMap: Record<string, { title: string; html: string }> = {
  conversations: {
    title: '会话说明',
    html: rules([
      '完整对话记录;点开一行查看消息明细与链路追踪。',
      '数据范围见顶部时间区间,点「刷新」拉取最新。',
    ]),
  },
  tasks: {
    title: '任务说明',
    html: rules([
      '后台任务的执行状态与产物;失败任务可查看失败原因。',
    ]),
  },
  detail: {
    title: '调用明细说明',
    html: rules([
      '每次 LLM / 上游服务调用的请求与响应明细,用于排查链路问题。',
    ]),
  },
  stats: {
    title: '环节统计说明',
    html: rules([
      '按环节(模型/上游/检索等)汇总调用量、耗时与费用,辅助定位慢点与成本。',
    ]),
  },
  audit: {
    title: '审计日志说明',
    html: rules([
      '管理端操作审计:配置变更、导入导出等关键动作全量留痕。',
    ]),
  },
}
const activityHelp = computed(() => activityHelpMap[activityTab.value] || activityHelpMap.conversations)
const activityHelpHtml = computed(() => activityHelp.value.html)
const activityHelpTitle = computed(() => activityHelp.value.title)

// 一级导航:概览 → 集成 → 记录 + pack 动态页。「集成」承载插件启停/
// MCP 服务/模型供应商(共同点是"接入什么、怎么接");「记录」汇聚会话/
// 任务/调用/审计(共同点是"发生过的事")
const navItems = computed(() => [
  { key: 'overview', label: '概览', icon: DashboardOutlined },
  { key: 'packs', label: '集成', icon: AppstoreOutlined },
  { key: 'activity', label: '记录', icon: MessageOutlined },
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
    // 智能客服-测试:跟随 metrology_assistant 插件启停
    const metro = data.items.find((p) => p.name === 'metrology_assistant')
    if (metro?.enabled && metro.dependency?.status === 'ok') {
      packPages.value.push({ pageKey: '__metrology_test', title: '智能客服-测试', component: MetrologyTestApp })
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

onMounted(() => {
  loadPackPages()
})
</script>

<style scoped>
.merged-tabs { display: flex; flex-direction: column; gap: 14px; }
.merged-tabs-row { display: flex; }
.shell {
  min-height: 100vh;
  background: var(--bg);
}
.shell-header {
  background: #fff;
  border-bottom: 1px solid var(--line);
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
  background: var(--brand-ink);
  color: #fff;
  font-size: 15px;
  font-weight: 700;
  letter-spacing: .02em;
  display: flex;
  align-items: center;
  justify-content: center;
}
.shell-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--ink);
  white-space: nowrap;
}
.ver {
  font-size: 12px;
  color: var(--ink-3);
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
  color: var(--brand-ink);
  background: var(--brand-tint-2);
}
.nav-item.active {
  color: var(--brand-ink);
  background: var(--brand-tint);
  font-weight: 600;
}
.nav-icon {
  font-size: 15px;
}
.shell-actions {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
}
/* 服务状态:绿点呼吸胶囊(替代旧 antd 蓝色转圈徽标) */
.svc-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  color: var(--ink-2);
  background: var(--ok-tint);
  border-radius: 999px;
  padding: 4px 12px 4px 10px;
}
.svc-pill i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 3px rgba(46, 161, 33, .14);
  animation: svc-pulse 2.8s ease-out infinite;
}
@keyframes svc-pulse {
  0% { box-shadow: 0 0 0 3px rgba(46, 161, 33, .18); }
  70% { box-shadow: 0 0 0 6px rgba(46, 161, 33, 0); }
  100% { box-shadow: 0 0 0 3px rgba(46, 161, 33, 0); }
}
@media (prefers-reduced-motion: reduce) {
  .svc-pill i { animation: none; }
}

.shell-main {
  max-width: 1760px;   /* 宽屏自适应:1400 在 2K+ 显示器两侧各留 ~580px 空白(用户 10-15 报"间距太大") */
  width: 100%;
  margin: 0 auto;
  padding: 16px 20px 0;
}
.page-card {
  background: var(--card);
  border: 1px solid var(--line-soft);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-rest);
  padding: 8px 20px 20px;
  min-height: calc(100vh - 140px);
  min-width: 0;
}
.shell-footer {
  text-align: center;
  color: var(--ink-3);
  font-size: 12.5px;
  padding: 18px 0 10px;
}
@media (max-width: 700px) {
  .shell-header { height: auto; padding: 10px 12px; gap: 10px; flex-wrap: wrap; }
  .top-nav { order: 3; flex-basis: 100%; }
  .shell-brand { min-width: 0; flex: 1; }
  .shell-title { font-size: 12px; }
  .shell-actions { gap: 8px; }
  .svc-pill { display: none; }
  .shell-main { padding: 10px 8px; }
  .page-card { padding: 8px 10px 12px; }
}
</style>
