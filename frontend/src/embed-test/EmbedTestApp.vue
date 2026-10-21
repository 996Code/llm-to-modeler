<template>
  <div class="et">
    <header class="et-head">
      <div class="et-title">
        <SafetyOutlined class="et-icon" />
        <div>
          <div class="et-name">嵌入数据权限测试</div>
          <div class="et-sub">身份注入 → 对话 ACL 判定 · 仅测试用</div>
        </div>
      </div>
      <a href="/ai-modeler/admin.html" target="_blank" class="et-link">
        <SettingOutlined /> 管理端 · 数据权限配置
      </a>
    </header>

    <div class="et-body">
      <!-- 左栏:身份与权限 -->
      <aside class="et-side">
        <section class="et-box">
          <div class="et-box-title">测试身份</div>
          <label class="et-field">
            <span>用户</span>
            <a-select v-model:value="userId" placeholder="不传 user_id" allow-clear
                      :options="userOptions" @change="dirty = true" />
          </label>
          <label class="et-field">
            <span>角色（可多选）</span>
            <a-select v-model:value="pickedRoles" mode="multiple" placeholder="不传角色"
                      :options="roleOptions" :max-tag-count="4" @change="dirty = true" />
          </label>
          <button class="et-btn" :disabled="!dirty" @click="reload">
            <ReloadOutlined /> {{ dirty ? '重载对话窗' : '已同步' }}
          </button>
          <div class="et-note">
            <InfoCircleOutlined />
            身份变化后点「重载对话窗」，对话窗整体重建并以新身份请求。
          </div>
        </section>

        <section class="et-box">
          <div class="et-box-title">可访问的知识库</div>
          <div v-if="!userId && !pickedRoles.length" class="et-note">
            <InfoCircleOutlined /> 未选身份：不做权限过滤，全部开放。
          </div>
          <ul class="et-kbs">
            <li v-for="kb in kbAcl" :key="kb.id" :class="allowed(kb) ? 'ok' : 'no'">
              <CheckCircleFilled v-if="allowed(kb)" />
              <CloseCircleFilled v-else />
              <span class="et-kb-name" :title="kb.name">{{ kb.name }}</span>
              <span class="et-kb-why">{{ why(kb) }}</span>
            </li>
          </ul>
          <div class="et-note"><InfoCircleOutlined /> 未配权限的库默认开放；配了则用户或任一角色命中才可访问。</div>
        </section>
      </aside>

      <!-- 右侧:宿主舞台 + 悬浮窗 -->
      <div class="et-stage">
        <div class="et-host">
          <div class="et-host-bar">
            <span class="dot" /><span class="dot" /><span class="dot" />
            <span class="et-host-url">https://host.example.com/workbench</span>
            <span class="et-badge">模拟宿主页面</span>
          </div>
          <div class="et-host-body">
            <div class="skl" style="width: 42%" />
            <div class="skl" style="width: 68%" />
            <div class="skl" style="width: 55%" />
            <p class="et-host-tip">宿主业务区。右侧为生产形态的 AI 悬浮窗（420px 嵌入）。</p>
          </div>
          <iframe v-if="frameKey" :key="frameKey" class="et-frame" :src="frameSrc" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 嵌入数据权限测试页:宿主角色/用户字典(subjects)+每库 ACL 从后端拉取,
// 身份经 iframe URL 传给嵌入对话(?embed=true&userId=&roles=),主应用把
// roles 装进 pack_params.knowledge_graph.user_roles 走 ACL 判定。
// 右侧按生产悬浮窗形态渲染(420px 窄容器),与真实宿主嵌入观感一致。
import { computed, onMounted, ref } from 'vue'
import axios from 'axios'
import {
  CheckCircleFilled, CloseCircleFilled, InfoCircleOutlined,
  ReloadOutlined, SafetyOutlined, SettingOutlined,
} from '@ant-design/icons-vue'

const KG = '/ai-modeler/api/packs/knowledge_graph'
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

const userOptions = computed(() =>
  users.value.map(u => ({ value: u.id, label: u.name && u.name !== u.id ? `${u.name}（${u.id}）` : u.id })))
const roleOptions = computed(() =>
  roles.value.map(r => ({ value: r.id, label: r.name && r.name !== r.id ? `${r.name}（${r.id}）` : r.id })))

const frameSrc = computed(() => {
  const p = new URLSearchParams({ embed: 'true', hostless: '1', packs: 'knowledge_graph' })
  if (userId.value) p.set('userId', userId.value)
  if (pickedRoles.value.length) p.set('roles', pickedRoles.value.join(','))
  return `/ai-modeler/?${p.toString()}`
})

function allowed(kb: KbAcl): boolean {
  if (!userId.value && !pickedRoles.value.length) return true
  if (!kb.users.length && !kb.roles.length) return true
  if (userId.value && kb.users.includes(userId.value)) return true
  return pickedRoles.value.some(r => kb.roles.includes(r))
}

/** 为什么可/不可访问(悬浮说明):开放/用户命中/角色命中/未命中任何授权 */
function why(kb: KbAcl): string {
  if (!userId.value && !pickedRoles.value.length) return '开放'
  if (!kb.users.length && !kb.roles.length) return '未配权限'
  if (userId.value && kb.users.includes(userId.value)) return '用户命中'
  const hit = pickedRoles.value.find(r => kb.roles.includes(r))
  if (hit) return `角色「${hit}」`
  return '未授权'
}

function reload() {
  dirty.value = false
  frameKey.value = `${userId.value || ''}|${pickedRoles.value.join(',')}|${Date.now()}`
}

onMounted(async () => {
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
    kbAcl.value = []
  }
})
</script>

<style scoped>
.et { padding: 16px 20px; font-size: 13px; color: #1f2937; }
.et-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 14px;
}
.et-title { display: flex; align-items: center; gap: 10px; }
.et-icon { font-size: 22px; color: #3370ff; }
.et-name { font-size: 16px; font-weight: 600; }
.et-sub { font-size: 12px; color: #9ca3af; }
.et-link { color: #2563eb; text-decoration: none; display: inline-flex; gap: 5px; align-items: center; }

.et-body { display: flex; gap: 14px; align-items: flex-start; }

/* 左栏 */
.et-side { width: 280px; flex: none; display: flex; flex-direction: column; gap: 12px; }
.et-box {
  border: 1px solid #e5e7eb; border-radius: 10px; background: #fff; padding: 12px 14px;
}
.et-box-title { font-weight: 600; margin-bottom: 10px; }
.et-field { display: block; margin-bottom: 10px; }
.et-field > span { display: block; font-size: 12px; color: #6b7280; margin-bottom: 4px; }
.et-field :deep(.ant-select) { width: 100%; }
.et-btn {
  width: 100%; border: none; border-radius: 8px; padding: 8px 0; cursor: pointer;
  background: #3370ff; color: #fff; font-size: 13px;
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
}
.et-btn:disabled { background: #e5e7eb; color: #9ca3af; cursor: default; }
.et-note {
  margin-top: 8px; font-size: 12px; color: #9ca3af; line-height: 1.5;
  display: flex; gap: 5px;
}
.et-kbs { list-style: none; margin: 0 0 6px; padding: 0; }
.et-kbs li {
  display: flex; align-items: center; gap: 7px; padding: 6px 0;
  border-bottom: 1px dashed #f0f1f4; font-size: 12.5px;
}
.et-kbs li:last-child { border-bottom: none; }
.et-kbs li.ok svg { color: #16a34a; }
.et-kbs li.no { color: #9ca3af; }
.et-kbs li.no svg { color: #dc2626; }
.et-kbs li.no .et-kb-name { text-decoration: line-through; }
.et-kb-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.et-kb-why { margin-left: auto; flex: none; font-size: 11px; color: #9ca3af; }

/* 右侧宿主舞台 */
.et-stage { flex: 1; min-width: 0; }
.et-host {
  position: relative; height: 700px; border: 1px solid #e5e7eb; border-radius: 10px;
  background: #f6f7f9; overflow: hidden;
}
.et-host-bar {
  display: flex; align-items: center; gap: 6px; padding: 9px 14px;
  background: #eceef2; border-bottom: 1px solid #e0e3e8;
}
.et-host-bar .dot { width: 10px; height: 10px; border-radius: 50%; background: #c3c8d4; }
.et-host-url { margin-left: 10px; color: #8a919e; font-size: 12px; }
.et-badge {
  margin-left: auto; font-size: 11px; color: #6b7280;
  background: #fff; border: 1px solid #e0e3e8; border-radius: 999px; padding: 1px 9px;
}
.et-host-body { padding: 22px 26px; }
.skl { height: 12px; border-radius: 6px; background: #e5e8ee; margin-bottom: 12px; }
.et-host-tip { color: #a8afba; font-size: 12px; margin-top: 22px; }

/* 悬浮窗(生产形态 420px) */
.et-frame {
  position: absolute; top: 54px; right: 16px; width: 420px; height: 626px;
  border: 1px solid #d9dde4; border-radius: 10px; background: #fff;
  box-shadow: 0 10px 30px rgba(15, 23, 42, 0.12);
}
</style>
