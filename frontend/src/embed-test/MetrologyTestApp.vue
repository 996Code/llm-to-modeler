<template>
  <div class="mt">
    <header class="mt-header">
      <div class="mt-heading"><CustomerServiceOutlined /><h1>智能客服测试</h1><a-tag>演示环境</a-tag></div>
      <div class="mt-actions">
        <span class="mt-status" :class="{ ready: environmentReady }"><span />{{ environmentStatus }}</span>
        <a-tooltip title="刷新环境状态"><a-button aria-label="刷新环境状态" :loading="loading" @click="load"><template #icon><ReloadOutlined /></template></a-button></a-tooltip>
      </div>
    </header>
    <div class="mt-workspace">
      <aside class="mt-sidebar">
        <a-tabs v-model:active-key="activeTab" class="flat-tabs">
          <a-tab-pane key="cases" tab="测试用例">
            <div class="mt-sidebar-content">
              <section class="mt-section">
                <h2>政策咨询</h2>
                <div v-for="item in policyCases" :key="item.title" class="mt-case">
                  <div class="mt-case-heading"><span>{{ item.title }}</span><a-tooltip title="复制测试问题"><a-button type="text" size="small" :aria-label="`复制${item.title}问题`" @click="copyQuestion(item.question)"><template #icon><CopyOutlined /></template></a-button></a-tooltip></div>
                  <p>{{ item.question }}</p><span class="mt-expected">{{ item.expected }}</span>
                </div>
              </section>
              <section class="mt-section">
                <h2>业务查询 <span class="mt-muted">模拟订单</span></h2>
                <a-skeleton v-if="loading && !env" :paragraph="{ rows: 3 }" active />
                <div v-for="order in env?.orderFixtures || []" :key="order.order" class="mt-order">
                  <div class="mt-order-detail"><code>{{ order.order }}</code><span>{{ order.desc }}</span></div>
                  <a-tooltip title="复制查询问题"><a-button type="text" size="small" :aria-label="`复制${order.order}查询`" @click="copyQuestion(`查询委托单 ${order.order} 的检测进度`)"><template #icon><CopyOutlined /></template></a-button></a-tooltip>
                </div>
                <a-alert v-if="error" :message="error" type="error" show-icon />
              </section>
            </div>
          </a-tab-pane>
          <a-tab-pane key="environment" tab="环境检查">
            <div class="mt-sidebar-content">
              <a-alert v-if="error" :message="error" type="error" show-icon />
              <a-skeleton v-else-if="!env" :paragraph="{ rows: 5 }" active />
              <template v-else>
                <a-alert v-for="item in env.errors || []" :key="item" :message="item" type="warning" show-icon class="mt-alert" />
                <section class="mt-section">
                  <h2>运行依赖</h2>
                  <div v-for="item in dependencies" :key="item.name" class="mt-dependency">
                    <CheckCircleFilled v-if="item.ok" class="mt-success" /><CloseCircleFilled v-else class="mt-warning" />
                    <div><strong>{{ item.name }}</strong><span>{{ item.detail }}</span></div>
                  </div>
                </section>
                <section class="mt-section">
                  <h2>资料范围</h2>
                  <div class="mt-rule"><strong>{{ env.kbMapping.mandatoryCheckKb }}</strong><div class="mt-purpose-tags"><a-tag v-for="purpose in env.kbMapping.purposes" :key="purpose">{{ purpose }}</a-tag></div></div>
                  <div class="mt-rule"><strong>{{ env.kbMapping.regulationKb }}</strong><p>其他用途 · 内部校准等</p></div>
                </section>
                <section class="mt-section"><h2>数据口径</h2><p>合成政策资料 · 模拟业务平台</p><p>真实资料与业务接口尚未联调</p><span v-if="checkedAt" class="mt-muted">检查于 {{ checkedAt }}</span></section>
              </template>
            </div>
          </a-tab-pane>
        </a-tabs>
      </aside>
      <section class="mt-conversation" aria-label="客服嵌入测试">
        <div class="mt-toolbar">
          <div class="mt-chat-label"><MessageOutlined /><span>嵌入预览</span></div>
          <span class="mt-embed-size">420px</span>
        </div>
        <div class="mt-preview"><iframe title="计量智能客服测试对话" class="mt-frame" :src="frameSrc" /></div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { CheckCircleFilled, CloseCircleFilled, CopyOutlined, CustomerServiceOutlined, MessageOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { adminApi } from '../admin/api'

interface EnvPayload {
  kbMapping: { purposes: string[]; mandatoryCheckKb: string; regulationKb: string }
  kbs: { name: string; exists: boolean; docs: number; imported: number }[]
  progress: {
    env_configured: boolean
    datasource: { name: string; active: boolean; dbType: string; host: string; tool: string } | null
    mcp: { code: string; status: string; enabled: boolean; tools: string[] } | null
  }
  orderFixtures: { order: string; desc: string }[]
  errors?: string[]
}
const policyCases = [
  { title: '缺条件追问', question: '压力表需要强制检定吗？', expected: '追问器具用途' },
  { title: '带依据回答', question: '压力表用于贸易结算，需要强制检定吗？', expected: '强检目录 · 片段引用' },
  { title: '变更用途', question: '那如果只是我们内部校准用的呢？', expected: '切换检定规程资料范围' },
]
const env = ref<EnvPayload | null>(null)
const loading = ref(false)
const error = ref('')
const checkedAt = ref('')
const activeTab = ref('cases')
const frameSrc = '/ai-modeler/?embed=true&hostless=1&packs=metrology_assistant'
let controller: AbortController | undefined

const dependencies = computed(() => {
  if (!env.value) return []
  const { kbs, progress } = env.value
  const ds = progress.datasource
  const mcp = progress.mcp
  return [
    ...kbs.map(kb => ({ name: kb.name, ok: kb.exists && kb.imported > 0, detail: kb.exists ? `${kb.imported} / ${kb.docs} 篇文档已导入` : '知识库未创建' })),
    { name: '业务平台 MCP', ok: !!mcp && mcp.status === 'connected' && mcp.enabled && mcp.tools.includes('query_progress'), detail: !mcp ? '未注册或状态不可用' : `${mcp.code} · ${mcp.enabled ? mcp.status : '已停用'}` },
    { name: '进度查询数据源', ok: !!ds && ds.active && ds.dbType === 'mcp' && ds.host === 'metrology' && ds.tool === 'query_progress', detail: !progress.env_configured ? '未配置数据源' : !ds ? '数据源不存在或不可用' : `${ds.name} · ${ds.active ? ds.tool : '已停用'}` },
  ]
})
const environmentReady = computed(() => !!env.value && !error.value && !env.value.errors?.length && dependencies.value.length === 4 && dependencies.value.every(item => item.ok))
const environmentStatus = computed(() => loading.value ? '检查中' : error.value ? '状态获取失败' : !env.value ? '未检查' : environmentReady.value ? '依赖就绪' : '依赖待检查')

async function copyQuestion(question: string) {
  try { await navigator.clipboard.writeText(question); message.success('测试问题已复制') }
  catch { message.error('复制失败，请检查浏览器剪贴板权限') }
}
async function load() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  controller = new AbortController()
  try {
    const { data } = await adminApi.get<EnvPayload>('/packs/metrology_assistant/test/env', { baseURL: '/ai-modeler/api', signal: controller.signal })
    env.value = data
    checkedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch {
    if (!controller.signal.aborted) { env.value = null; error.value = '无法获取环境状态，请检查服务后重试' }
  } finally { loading.value = false }
}
onMounted(load)
onBeforeUnmount(() => controller?.abort())
</script>

<style scoped>
.mt { color: #1f2329; padding: 20px; }
.mt-header, .mt-heading, .mt-actions, .mt-toolbar, .mt-chat-label { display: flex; align-items: center; }
.mt-header { justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.mt-heading { gap: 10px; min-width: 0; flex-wrap: wrap; }
.mt-heading > .anticon { color: #2563eb; font-size: 20px; }
.mt-heading h1 { font-size: 18px; font-weight: 600; margin: 0; }
.mt-heading :deep(.ant-tag) { margin: 0; color: #8c651a; background: #fff8e8; border-color: #f3e3bf; }
.mt-actions { gap: 14px; flex-shrink: 0; }
.mt-status { display: inline-flex; align-items: center; gap: 7px; color: #8c651a; font-size: 12px; }
.mt-status > span { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.mt-status.ready { color: #238453; }
.mt-workspace { display: grid; grid-template-columns: 280px minmax(0, 1fr); border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; height: max(520px, calc(100dvh - 210px)); }
.mt-sidebar { min-width: 0; min-height: 0; background: #fff; border-right: 1px solid #e5e7eb; overflow: auto; }
.mt-sidebar :deep(.ant-tabs-nav) { margin-bottom: 0; padding: 0 18px; }
.mt-sidebar :deep(.ant-tabs-nav-list) { gap: 24px; }
.mt-sidebar :deep(.ant-tabs-tab + .ant-tabs-tab) { margin-left: 0; }
.mt-sidebar-content { padding: 0 18px 18px; }
.mt-section { padding: 20px 0; border-bottom: 1px solid #eef0f3; }
.mt-section:last-child { border-bottom: 0; padding-bottom: 0; }
.mt-section h2 { margin: 0 0 12px; font-size: 12px; color: #626b78; font-weight: 600; }
.mt-muted { font-size: 11px; font-weight: 400; color: #8c939e; margin-left: 4px; }
.mt-case { padding: 10px 0; border-bottom: 1px solid #f0f1f4; }
.mt-case:last-child { border-bottom: 0; padding-bottom: 0; }
.mt-case:first-of-type { padding-top: 0; }
.mt-case-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 13px; font-weight: 500; }
.mt-case p, .mt-section > p { font-size: 12px; line-height: 1.7; margin: 3px 0 6px; color: #626b78; }
.mt-expected { font-size: 11px; color: #73809a; }
.mt-order { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 9px 0; }
.mt-order-detail { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.mt-order code { font-size: 12px; font-weight: 600; color: #374151; }
.mt-order-detail > span { font-size: 11px; color: #7d8592; overflow-wrap: anywhere; }
.mt-dependency { display: flex; align-items: flex-start; gap: 9px; margin: 16px 0; }
.mt-dependency > .anticon { margin-top: 3px; font-size: 14px; }
.mt-dependency > div { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.mt-dependency strong, .mt-rule strong { font-size: 13px; font-weight: 500; }
.mt-dependency div > span { font-size: 12px; color: #7d8592; overflow-wrap: anywhere; }
.mt-success { color: #238453; }
.mt-warning { color: #c47f1b; }
.mt-alert { margin-top: 12px; }
.mt-rule { margin-bottom: 16px; }
.mt-rule:last-child { margin-bottom: 0; }
.mt-purpose-tags { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 10px; }
.mt-purpose-tags :deep(.ant-tag) { margin: 0; font-size: 11px; background: #f7f8fa; color: #626b78; }
.mt-rule p { font-size: 12px; color: #7d8592; line-height: 1.8; margin: 6px 0 0; }
.mt-conversation { min-width: 0; min-height: 0; display: flex; flex-direction: column; background: #f5f6f8; }
.mt-toolbar { justify-content: space-between; flex-shrink: 0; gap: 12px; padding: 12px 16px; min-height: 54px; background: #fff; border-bottom: 1px solid #e5e7eb; flex-wrap: wrap; }
.mt-chat-label { gap: 8px; font-size: 13px; font-weight: 500; }
.mt-chat-label .anticon { color: #73809a; }
.mt-embed-size { font-size: 11px; color: #8c939e; }
.mt-preview { flex: 1; min-height: 0; display: flex; justify-content: flex-end; padding: 20px 24px; overflow: hidden; background: #f5f6f8; }
.mt-frame { display: block; flex: 0 1 420px; width: 420px; max-width: 100%; height: 100%; min-width: 0; background: #fff; border: 1px solid #dfe3e9; border-radius: 8px; box-shadow: 0 6px 20px rgb(31 35 41 / 8%); }
@media (max-width: 980px) {
  .mt { padding: 16px; }
  .mt-workspace { grid-template-columns: 244px minmax(0, 1fr); }
  .mt-sidebar-content { padding-left: 14px; padding-right: 14px; }
}
@media (max-width: 700px) {
  .mt { padding: 12px; }
  .mt-header { align-items: flex-start; gap: 10px; }
  .mt-heading { gap: 7px; }
  .mt-heading h1 { font-size: 16px; }
  .mt-actions { gap: 8px; }
  .mt-status { max-width: 80px; }
  .mt-workspace { display: flex; flex-direction: column; height: auto; }
  .mt-sidebar { border-right: 0; border-bottom: 1px solid #e5e7eb; max-height: 280px; }
  .mt-conversation { height: 640px; flex: none; }
  .mt-preview { padding: 10px; }
}
</style>
