<template>
  <div class="et">
    <header class="et-head">
      <b>嵌入数据权限测试</b>
      <span class="et-dim">身份经 URL(userId/roles)注入 iframe 对话 → ACL 判定；仅测试用</span>
      <a class="et-link" href="/ai-modeler/admin.html" target="_blank">管理端·数据权限配置 →</a>
    </header>

    <section class="et-panel">
      <div class="et-row">
        <span class="et-label">用户</span>
        <select v-model="userId" class="et-select">
          <option value="">（不传 user_id）</option>
          <option v-for="u in users" :key="u.id" :value="u.id">{{ u.name || u.id }}（{{ u.id }}）</option>
        </select>
        <span class="et-label">角色</span>
        <div class="et-roles">
          <label v-for="r in roles" :key="r.id" class="et-role" :class="{ on: pickedRoles.includes(r.id) }">
            <input type="checkbox" :value="r.id" v-model="pickedRoles" @change="dirty = true" />
            {{ r.name || r.id }}
          </label>
          <span v-if="!roles.length" class="et-dim">subjects 字典为空（宿主未同步/未播种），仍可手测：</span>
        </div>
        <button class="et-btn" :disabled="!dirty" @click="reload">重载对话窗</button>
      </div>

      <div class="et-preview">
        <span class="et-label">ACL 预览</span>
        <span v-for="kb in kbAcl" :key="kb.id" class="et-kb" :class="allowed(kb) ? 'ok' : 'no'">
          {{ kb.name }}{{ allowed(kb) ? ' ✓' : ' ✗' }}
        </span>
        <span v-if="!userId && !pickedRoles.length" class="et-dim">未选身份 → ACL 不过滤（全开放）</span>
      </div>
    </section>

    <iframe v-if="frameKey" :key="frameKey" class="et-frame" :src="frameSrc" />
    <p class="et-dim et-tip">
      预览语义与后端一致：库未配 ACL = 开放；配了 = user_id 或任一角色命中才放行。
      切换身份后点「重载对话窗」（或勾选角色后自动可用）——iframe 整体重建,对话窗干净重载。
    </p>
  </div>
</template>

<script setup lang="ts">
// 嵌入数据权限测试页:宿主角色/用户字典(subjects)+每库 ACL 从后端拉取,
// 身份经 iframe URL 传给嵌入对话(?embed=true&userId=&roles=),主应用把
// roles 装进 pack_params.knowledge_graph.user_roles 走 ACL 判定。
import { computed, onMounted, ref } from 'vue'
import axios from 'axios'

const KG = '/ai-modeler/api/packs/knowledge_graph'
const http = axios.create({ baseURL: '' })
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
const userId = ref('')
const pickedRoles = ref<string[]>([])
const dirty = ref(false)
const frameKey = ref('')

const frameSrc = computed(() => {
  const p = new URLSearchParams({ embed: 'true' })
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

function reload() {
  dirty.value = false
  frameKey.value = `${userId.value}|${pickedRoles.value.join(',')}|${Date.now()}`
}

onMounted(async () => {
  try {
    const kbs = (await http.get(`${KG}/kbs`)).data.items || []
    // 每库 ACL(含全局 subjects 候选,取一次即可)
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
    // 初始加载一次对话窗(无身份)
    reload()
  } catch (e) {
    kbAcl.value = []
  }
})
</script>

<style scoped>
.et { max-width: 1100px; margin: 0 auto; padding: 16px; font-size: 13px; }
.et-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.et-dim { color: #9ca3af; font-weight: 400; }
.et-link { margin-left: auto; color: #2563eb; }
.et-panel { border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px; background: #fafbfd; }
.et-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.et-label { font-weight: 600; color: #374151; }
.et-select { border: 1px solid #d1d5db; border-radius: 6px; padding: 4px 8px; }
.et-roles { display: flex; gap: 6px; flex-wrap: wrap; }
.et-role {
  border: 1px solid #d1d5db; border-radius: 999px; padding: 2px 10px;
  cursor: pointer; background: #fff;
}
.et-role.on { border-color: #2563eb; color: #2563eb; background: #eff6ff; }
.et-btn {
  border: none; background: #2563eb; color: #fff; border-radius: 6px;
  padding: 5px 14px; cursor: pointer;
}
.et-btn:disabled { background: #9ca3af; cursor: not-allowed; }
.et-preview { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.et-kb { border-radius: 6px; padding: 1px 8px; font-size: 12px; }
.et-kb.ok { background: #f0fdf4; color: #15803d; }
.et-kb.no { background: #fef2f2; color: #b91c1c; text-decoration: line-through; }
.et-frame {
  width: 100%; height: 640px; border: 1px solid #e5e7eb;
  border-radius: 10px; margin-top: 12px; background: #fff;
}
.et-tip { margin-top: 8px; }
</style>
