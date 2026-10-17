<template>
  <div class="acl">
    <div class="acl-row">
      <div class="acl-col">
        <div class="acl-col-title">允许的用户</div>
        <a-select v-model:value="users" mode="multiple" placeholder="从同步的用户中选择"
                  style="width: 100%" :loading="loading" :options="userOptions"
                  option-filter-prop="label" show-search allow-clear />
        <div class="acl-col-sub">候选来自宿主同步;未同步时列表为空</div>
      </div>
      <div class="acl-col">
        <div class="acl-col-title">允许的角色</div>
        <a-select v-model:value="roles" mode="multiple" placeholder="从同步的角色中选择"
                  style="width: 100%" :loading="loading" :options="roleOptions"
                  option-filter-prop="label" show-search allow-clear />
        <div class="acl-col-sub">命中任一角色即可检索</div>
      </div>
    </div>

    <div class="acl-actions">
      <a-button type="primary" :loading="saving" @click="save">保存</a-button>
      <a-button :disabled="saving || (!users.length && !roles.length)" @click="clear">
        清空(回到全开放)
      </a-button>
      <span v-if="saved" class="acl-saved">已保存</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// 检索数据权限编辑:每库允许的用户/角色(嵌入模式 ACL)。
// 候选不做手输——来自宿主同步接口写入的字典(POST /subjects/{type});
// 保存为全量替换,清空 = 回到全开放。
import { inject, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { fetchKgKbAcl, saveKgKbAcl } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const users = ref<string[]>([])
const roles = ref<string[]>([])
const userOptions = ref<{ value: string; label: string }[]>([])
const roleOptions = ref<{ value: string; label: string }[]>([])
const loading = ref(false)
const saving = ref(false)
const saved = ref(false)

async function load() {
  loading.value = true
  await loadSafely(async () => {
    const acl = await fetchKgKbAcl(props.kbId)
    users.value = acl.users || []
    roles.value = acl.roles || []
    userOptions.value = (acl.options?.users || []).map((u) => ({ value: u.id, label: u.name || u.id }))
    roleOptions.value = (acl.options?.roles || []).map((r) => ({ value: r.id, label: r.name || r.id }))
  })
  loading.value = false
}

watch(() => props.kbId, () => { saved.value = false; load() }, { immediate: true })

async function save() {
  saving.value = true
  try {
    await saveKgKbAcl(props.kbId, { users: users.value, roles: roles.value })
    message.success('数据权限已保存')
    saved.value = true
  } catch (e) {
    message.error((e as any)?.response?.data?.detail || '保存失败')
  } finally { saving.value = false }
}

async function clear() {
  users.value = []
  roles.value = []
  await save()
}
</script>

<style scoped>
.acl { max-width: 760px; }
.acl-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.acl-col { border: 1px solid #eef1f7; border-radius: 10px; padding: 10px 14px; background: #fff; }
.acl-col-title { font-weight: 600; font-size: 13px; color: #1f2937; margin-bottom: 8px; }
.acl-col-sub { font-size: 12px; color: #94a3b8; margin-top: 6px; }
.acl-actions { display: flex; gap: 8px; align-items: center; margin-top: 14px; }
.acl-saved { color: #16a34a; font-size: 12px; }
</style>
