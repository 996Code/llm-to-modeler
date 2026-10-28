<template>
  <div class="et">
    <header class="et-header">
      <div class="et-heading"><SafetyOutlined /><h1>知识库测试</h1><a-tag>数据权限</a-tag></div>
      <a href="/ai-modeler/admin.html" target="_blank" rel="noopener noreferrer" class="et-link">
        <SettingOutlined /> 权限配置
      </a>
    </header>
    <div class="et-workspace">
      <aside class="et-sidebar" :aria-busy="loading">
        <div v-if="loading" class="et-feedback" role="status"><LoadingOutlined spin /> 正在加载身份与权限范围</div>
        <a-alert v-else-if="error" class="et-feedback" :message="error" type="error" show-icon>
          <template #action><a-button size="small" @click="load"><template #icon><ReloadOutlined /></template>重试</a-button></template>
        </a-alert>
        <a-tabs v-model:active-key="activeTab" class="flat-tabs">
          <a-tab-pane key="identity" tab="测试身份">
            <div class="et-sidebar-content">
              <section class="et-section">
                <label class="et-field">
                  <span>用户</span>
                  <a-select v-model:value="userId" placeholder="不传 user_id" allow-clear
                            :options="userOptions" :disabled="loading || !!error" @change="dirty = true" />
                </label>
                <label class="et-field">
                  <span>角色（可多选）</span>
                  <a-select v-model:value="pickedRoles" mode="multiple" placeholder="不传角色"
                            :options="roleOptions" :max-tag-count="4" :disabled="loading || !!error" @change="dirty = true" />
                </label>
                <a-button type="primary" block :disabled="!dirty || loading || !!error" @click="reload">
                  <template #icon><ReloadOutlined /></template>{{ dirty ? '重载对话窗' : '已同步' }}
                </a-button>
                <p v-if="dirty" class="et-note">身份待同步</p>
              </section>
            </div>
          </a-tab-pane>
          <a-tab-pane key="permissions" tab="权限范围">
            <div class="et-sidebar-content">
              <section v-if="!loading && !error" class="et-section">
                <h2>可访问的知识库 <span class="et-muted">{{ kbAcl.filter(allowed).length }} / {{ kbAcl.length }}</span></h2>
                <p v-if="!userId && !pickedRoles.length" class="et-note">未选身份 · 全部开放</p>
                <p v-else-if="dirty" class="et-note">当前选择的权限预览 · 身份待同步</p>
                <ul v-if="kbAcl.length" class="et-kbs">
                  <li v-for="kb in kbAcl" :key="kb.id" :class="allowed(kb) ? 'ok' : 'no'">
                    <CheckCircleFilled v-if="allowed(kb)" /><CloseCircleFilled v-else />
                    <span class="et-kb-name" :title="kb.name">{{ kb.name }}</span>
                    <span class="et-kb-why">{{ why(kb) }}</span>
                  </li>
                </ul>
                <a-empty v-else description="暂无知识库" :image="simpleImage" />
                <p v-if="kbAcl.length" class="et-note">未配权限默认开放；用户或任一角色命中可访问。</p>
              </section>
            </div>
          </a-tab-pane>
        </a-tabs>
      </aside>
      <section class="et-conversation" aria-label="知识库嵌入测试">
        <div class="et-toolbar">
          <div class="et-chat-label"><MessageOutlined /><span>嵌入预览</span></div>
          <span class="et-embed-size">420px</span>
        </div>
        <div class="et-preview">
          <iframe v-if="frameKey" :key="frameKey" title="知识库测试对话" class="et-frame" :src="frameSrc" />
          <div v-else class="et-preview-status" :role="error ? 'alert' : 'status'">
            <template v-if="error"><CloseCircleFilled /><span>嵌入预览未加载</span></template>
            <template v-else><LoadingOutlined spin /><span>正在加载嵌入预览</span></template>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import axios from 'axios'
import { Empty } from 'ant-design-vue'
import {
  CheckCircleFilled, CloseCircleFilled, LoadingOutlined, MessageOutlined,
  ReloadOutlined, SafetyOutlined, SettingOutlined,
} from '@ant-design/icons-vue'

const KG = '/ai-modeler/api/packs/knowledge_graph'
const simpleImage = Empty.PRESENTED_IMAGE_SIMPLE
const http = axios.create()
http.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('auth_token')
  if (token) cfg.headers['Authorization'] = `Bearer ${token}`
  return cfg
})

interface Subject { id: string; name: string }
interface KbAcl { id: string; name: string; users: string[]; roles: string[] }

const users = ref<Subject[]>([])
const roles = ref<Subject[]>([])
const kbAcl = ref<KbAcl[]>([])
const userId = ref<string | undefined>(undefined)
const pickedRoles = ref<string[]>([])
const dirty = ref(false)
const frameKey = ref('')
const loading = ref(false)
const error = ref('')
const activeTab = ref('identity')
const appliedIdentity = ref<{ userId?: string; roles: string[] }>({ roles: [] })
let reloadCount = 0

const userOptions = computed(() =>
  users.value.map(u => ({ value: u.id, label: u.name && u.name !== u.id ? `${u.name}（${u.id}）` : u.id })))
const roleOptions = computed(() =>
  roles.value.map(r => ({ value: r.id, label: r.name && r.name !== r.id ? `${r.name}（${r.id}）` : r.id })))

const frameSrc = computed(() => {
  const p = new URLSearchParams({ embed: 'true', hostless: '1', packs: 'knowledge_graph' })
  if (appliedIdentity.value.userId) p.set('userId', appliedIdentity.value.userId)
  if (appliedIdentity.value.roles.length) p.set('roles', appliedIdentity.value.roles.join(','))
  return `/ai-modeler/?${p.toString()}`
})

function allowed(kb: KbAcl): boolean {
  if (!userId.value && !pickedRoles.value.length) return true
  if (!kb.users.length && !kb.roles.length) return true
  if (userId.value && kb.users.includes(userId.value)) return true
  return pickedRoles.value.some(r => kb.roles.includes(r))
}

function why(kb: KbAcl): string {
  if (!userId.value && !pickedRoles.value.length) return '开放'
  if (!kb.users.length && !kb.roles.length) return '未配权限'
  if (userId.value && kb.users.includes(userId.value)) return '用户命中'
  const hit = pickedRoles.value.find(r => kb.roles.includes(r))
  if (hit) return `角色「${hit}」`
  return '未授权'
}

function reload() {
  appliedIdentity.value = { userId: userId.value, roles: [...pickedRoles.value] }
  dirty.value = false
  frameKey.value = String(++reloadCount)
}

async function load() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const kbs = (await http.get(`${KG}/kbs`)).data.items || []
    let opts: { users?: Subject[]; roles?: Subject[] } | null = null
    const acls: KbAcl[] = []
    for (const kb of kbs) {
      const r = (await http.get(`${KG}/kbs/${kb.id}/acl`)).data
      opts = opts || r.options
      acls.push({ id: kb.id, name: kb.name, users: r.users || [], roles: r.roles || [] })
    }
    users.value = opts?.users || []
    roles.value = opts?.roles || []
    kbAcl.value = acls
    reload()
  } catch {
    users.value = []
    roles.value = []
    kbAcl.value = []
    frameKey.value = ''
    error.value = '无法加载身份与权限范围，请检查服务后重试'
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<style scoped>
.et { color: #1f2329; padding: 20px; font-size: 13px; }
.et-header, .et-heading, .et-toolbar, .et-chat-label { display: flex; align-items: center; }
.et-header { justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.et-heading { gap: 10px; min-width: 0; flex-wrap: wrap; }
.et-heading > .anticon { color: #2563eb; font-size: 20px; }
.et-heading h1 { font-size: 18px; font-weight: 600; margin: 0; }
.et-heading :deep(.ant-tag) { margin: 0; color: #8c651a; background: #fff8e8; border-color: #f3e3bf; }
.et-link { color: #2563eb; text-decoration: none; display: inline-flex; gap: 5px; align-items: center; flex-shrink: 0; }
.et-workspace { display: grid; grid-template-columns: 280px minmax(0, 1fr); border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; height: max(520px, calc(100dvh - 210px)); }
.et-sidebar { min-width: 0; min-height: 0; background: #fff; border-right: 1px solid #e5e7eb; overflow: auto; }
.et-sidebar :deep(.ant-tabs-nav) { margin-bottom: 0; padding: 0 18px; }
.et-sidebar :deep(.ant-tabs-nav-list) { gap: 24px; }
.et-sidebar :deep(.ant-tabs-tab + .ant-tabs-tab) { margin-left: 0; }
.et-sidebar-content { padding: 0 18px 18px; }
.et-section { padding: 20px 0; }
.et-section h2 { margin: 0 0 12px; font-size: 12px; color: #626b78; font-weight: 600; }
.et-muted { font-size: 11px; font-weight: 400; color: #8c939e; margin-left: 4px; }
.et-field { display: block; margin-bottom: 16px; }
.et-field > span { display: block; font-size: 12px; color: #626b78; margin-bottom: 6px; }
.et-field :deep(.ant-select) { width: 100%; }
.et-note { margin: 10px 0 0; font-size: 12px; color: #7d8592; line-height: 1.7; }
.et-feedback { margin: 14px 14px 0; font-size: 12px; overflow-wrap: anywhere; }
.et-kbs { list-style: none; margin: 8px 0; padding: 0; }
.et-kbs li { display: flex; align-items: center; gap: 7px; padding: 12px 0; border-bottom: 1px solid #f0f1f4; font-size: 12px; }
.et-kbs li:last-child { border-bottom: none; }
.et-kbs li > .anticon { flex-shrink: 0; }
.et-kbs li.ok > .anticon { color: #238453; }
.et-kbs li.no { color: #8c939e; }
.et-kbs li.no > .anticon { color: #c47f1b; }
.et-kb-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.et-kb-why { margin-left: auto; flex-shrink: 0; max-width: 50%; overflow-wrap: anywhere; text-align: right; font-size: 11px; color: #7d8592; }
.et-conversation { min-width: 0; min-height: 0; display: flex; flex-direction: column; background: #f5f6f8; }
.et-toolbar { justify-content: space-between; flex-shrink: 0; gap: 12px; padding: 12px 16px; min-height: 54px; background: #fff; border-bottom: 1px solid #e5e7eb; flex-wrap: wrap; }
.et-chat-label { gap: 8px; font-size: 13px; font-weight: 500; }
.et-chat-label > .anticon { color: #73809a; }
.et-embed-size { font-size: 11px; color: #8c939e; }
.et-preview { flex: 1; min-height: 0; display: flex; justify-content: flex-end; padding: 20px 24px; overflow: hidden; }
.et-frame { display: block; flex: 0 1 420px; width: 420px; max-width: 100%; height: 100%; min-width: 0; background: #fff; border: 1px solid #dfe3e9; border-radius: 8px; box-shadow: 0 6px 20px rgb(31 35 41 / 8%); }
.et-preview-status { display: flex; flex: 0 1 420px; min-width: 0; align-items: center; justify-content: center; gap: 8px; color: #7d8592; }
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
  .et-sidebar { border-right: 0; border-bottom: 1px solid #e5e7eb; max-height: 280px; }
  .et-conversation { height: 640px; flex: none; }
  .et-preview { padding: 10px; }
}
</style>
