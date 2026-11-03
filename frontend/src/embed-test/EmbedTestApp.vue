<template>
  <div class="et">
    <header class="et-header">
      <div class="et-heading"><SafetyOutlined /><h1>知识库测试</h1><a-tag>嵌入预览</a-tag></div>
      <a href="/ai-modeler/admin.html" target="_blank" rel="noopener noreferrer" class="et-link">
        <SettingOutlined /> 知识库管理
      </a>
    </header>
    <div class="et-workspace">
      <aside class="et-sidebar" :aria-busy="loading">
        <div v-if="loading" class="et-feedback" role="status"><LoadingOutlined spin /> 正在加载知识库清单</div>
        <a-alert v-else-if="error" class="et-feedback" :message="error" type="error" show-icon>
          <template #action><a-button size="small" @click="load"><template #icon><ReloadOutlined /></template>重试</a-button></template>
        </a-alert>
        <div v-else class="et-sidebar-content">
          <section class="et-section">
            <h2>知识库 <span class="et-muted">{{ kbs.length }} 个</span></h2>
            <p v-if="!kbs.length" class="et-note">暂无知识库，请先在知识库管理中创建</p>
            <ul v-else class="et-kbs">
              <li v-for="kb in kbs" :key="kb.id" class="ok">
                <DatabaseOutlined />
                <span class="et-kb-name" :title="kb.name">{{ kb.name }}</span>
                <span class="et-kb-why">{{ kb.docCount }} 文档</span>
              </li>
            </ul>
          </section>
        </div>
      </aside>
      <section class="et-conversation" aria-label="知识库嵌入测试">
        <div class="et-preview">
          <iframe v-if="frameKey && frameOpen" :key="frameKey" title="知识库测试对话" class="et-frame" :src="frameSrc" />
          <div v-else class="et-preview-status" :role="error ? 'alert' : 'status'">
            <template v-if="error"><CloseCircleFilled /><span>嵌入预览未加载</span></template>
            <template v-else-if="!frameOpen">
              <span>对话窗口已收起</span>
              <button type="button" class="et-reopen" @click="reopenFrame">重新打开</button>
            </template>
            <template v-else><LoadingOutlined spin /><span>正在加载嵌入预览</span></template>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import axios from 'axios'
import {
  CloseCircleFilled, DatabaseOutlined, LoadingOutlined,
  ReloadOutlined, SafetyOutlined, SettingOutlined,
} from '@ant-design/icons-vue'

const KG = '/ai-modeler/api/packs/knowledge_graph'
const http = axios.create()
http.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('auth_token')
  if (token) cfg.headers['Authorization'] = `Bearer ${token}`
  return cfg
})

interface Kb { id: string; name: string; docCount: number }

const kbs = ref<Kb[]>([])
const frameKey = ref('')
const frameOpen = ref(true)
const loading = ref(false)
const error = ref('')
let reloadCount = 0

const frameSrc = computed(() => {
  const p = new URLSearchParams({ embed: 'true', hostless: '1', packs: 'knowledge_graph' })
  return `/ai-modeler/?${p.toString()}`
})

function reload() {
  frameKey.value = String(++reloadCount)
}

async function load() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const items = (await http.get(`${KG}/kbs`)).data.items || []
    kbs.value = items.map((k: { id: string; name: string; doc_count?: number; docCount?: number }) =>
      ({ id: k.id, name: k.name, docCount: k.docCount ?? k.doc_count ?? 0 }))
    reload()
  } catch {
    kbs.value = []
    frameKey.value = ''
    error.value = '无法加载知识库清单，请检查服务后重试'
  } finally {
    loading.value = false
  }
}
// 宿主侧 CLOSE 处理:iframe 内点 ✕ 发 postMessage(hostless 无 SDK 宿主,
// 测试页自己充当宿主)——收起预览并提供重新打开入口
function reopenFrame() {
  frameOpen.value = true
}
function onChildMessage(e: MessageEvent) {
  const d = e.data as { src?: string; type?: string } | null
  if (d && d.src === 'child' && d.type === 'CLOSE') frameOpen.value = false
}
if (typeof window !== 'undefined') {
  window.addEventListener('message', onChildMessage)
  onBeforeUnmount(() => window.removeEventListener('message', onChildMessage))
}
onMounted(load)
</script>

<style scoped>
.et { color: var(--ink); padding: 20px; font-size: 14px; }
.et-header, .et-heading { display: flex; align-items: center; }
.et-header { justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.et-heading { gap: 10px; min-width: 0; flex-wrap: wrap; }
.et-heading > .anticon { color: var(--brand); font-size: 20px; }
.et-heading h1 { font-size: 18px; font-weight: 650; margin: 0; }
.et-heading :deep(.ant-tag) { margin: 0; color: var(--brand-ink); background: var(--brand-tint); border-color: #C9D9FF; }
.et-link { color: var(--brand-ink); text-decoration: none; display: inline-flex; gap: 5px; align-items: center; flex-shrink: 0; }
.et-workspace { display: grid; grid-template-columns: 280px minmax(0, 1fr); border: 1px solid var(--line-soft); border-radius: var(--radius-card); overflow: hidden; height: max(520px, calc(100dvh - 210px)); }
.et-sidebar { min-width: 0; min-height: 0; background: var(--card); border-right: 1px solid var(--line-soft); overflow: auto; }
.et-sidebar-content { padding: 0 18px 18px; }
.et-section { padding: 20px 0; }
.et-section h2 { margin: 0 0 12px; font-size: 13px; color: var(--ink-2); font-weight: 600; }
.et-muted { font-size: 12px; font-weight: 400; color: var(--ink-3); margin-left: 4px; }
.et-note { margin: 10px 0 0; font-size: 12.5px; color: var(--ink-3); line-height: 1.7; }
.et-feedback { margin: 14px 14px 0; font-size: 13px; overflow-wrap: anywhere; }
.et-kbs { list-style: none; margin: 8px 0; padding: 0; }
.et-kbs li { display: flex; align-items: center; gap: 7px; padding: 12px 0; border-bottom: 1px solid var(--line-soft); font-size: 13px; }
.et-kbs li:last-child { border-bottom: none; }
.et-kbs li > .anticon { flex-shrink: 0; color: var(--brand); }
.et-kb-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.et-kb-why { margin-left: auto; flex-shrink: 0; max-width: 50%; overflow-wrap: anywhere; text-align: right; font-size: 12px; color: var(--ink-3); }
.et-conversation { min-width: 0; min-height: 0; display: flex; flex-direction: column; background: var(--bg); }
.et-preview { flex: 1; min-height: 0; display: flex; justify-content: flex-end; padding: 20px 24px; overflow: hidden; }
.et-frame { display: block; flex: 0 1 420px; width: 420px; max-width: 100%; height: 100%; min-width: 0; background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-card); box-shadow: var(--shadow-lift); }
.et-preview-status { display: flex; flex: 0 1 420px; min-width: 0; align-items: center; justify-content: center; gap: 8px; color: var(--ink-3); }
.et-reopen { border: 1px solid var(--line); border-radius: var(--radius-ctrl); background: var(--card); color: var(--brand-ink); padding: 4px 12px; cursor: pointer; }
.et-reopen:hover { border-color: var(--brand); }
@media (max-width: 980px) {
  .et { padding: 16px; }
  .et-workspace { grid-template-columns: 244px minmax(0, 1fr); }
  .et-sidebar-content { padding-left: 14px; padding-right: 14px; }
}
@media (max-width: 700px) {
  .et { padding: 12px; }
  .et-header { align-items: flex-start; gap: 10px; }
  .et-heading { gap: 7px; }
  .et-heading h1 { font-size: 16px; }
  .et-workspace { display: flex; flex-direction: column; height: auto; }
  .et-sidebar { border-right: 0; border-bottom: 1px solid var(--line-soft); max-height: 280px; }
  .et-conversation { height: 640px; flex: none; }
  .et-preview { padding: 10px; }
}
</style>
