<template>
  <div class="acl">
    <a-alert type="info" show-icon style="margin-bottom: 14px">
      <template #message>检索数据权限(嵌入模式)</template>
      <template #description>
        配置后,仅当宿主传入用户身份(X-User-Id)或角色(pack_params 的 user_roles)时才生效;
        <b>不配置 = 所有人可检索</b>;未传身份时不过滤。管理端页面不受此限制。
      </template>
    </a-alert>

    <div class="acl-row">
      <div class="acl-col">
        <div class="acl-title">允许的用户</div>
        <div class="acl-sub">命中任一用户即可检索(用户 ID,与宿主 X-User-Id 一致)</div>
        <a-select v-model:value="users" mode="tags" placeholder="输入用户 ID 回车添加"
                  style="width: 100%" :open="false" :token-separators="[',']"
                  :disabled="loading" />
      </div>
      <div class="acl-col">
        <div class="acl-title">允许的角色</div>
        <div class="acl-sub">命中任一角色即可检索(与宿主透传的 user_roles 一致)</div>
        <a-select v-model:value="roles" mode="tags" placeholder="输入角色名回车添加"
                  style="width: 100%" :open="false" :token-separators="[',']"
                  :disabled="loading" />
      </div>
    </div>

    <div class="acl-actions">
      <a-button type="primary" :loading="saving" @click="save">保存</a-button>
      <a-button :disabled="saving || (!users.length && !roles.length)" @click="clear">清空(回到全开放)</a-button>
      <span v-if="saved" class="acl-saved">已保存</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// 检索数据权限编辑:每库允许的用户/角色(嵌入模式 ACL)。
// 保存为全量替换;清空两个列表 = 回到全开放。
import { inject, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { KgKbAcl, fetchKgKbAcl, saveKgKbAcl } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'

const props = defineProps<{ kbId: string }>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const users = ref<string[]>([])
const roles = ref<string[]>([])
const loading = ref(false)
const saving = ref(false)
const saved = ref(false)

async function load() {
  loading.value = true
  await loadSafely(async () => {
    const acl = await fetchKgKbAcl(props.kbId)
    users.value = acl.users || []
    roles.value = acl.roles || []
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
.acl { max-width: 720px; }
.acl-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.acl-col { border: 1px solid #eef1f7; border-radius: 10px; padding: 10px 14px; background: #fff; }
.acl-title { font-weight: 600; font-size: 13px; color: #1f2937; }
.acl-sub { font-size: 12px; color: #94a3b8; margin: 2px 0 8px; line-height: 1.5; }
.acl-actions { display: flex; gap: 8px; align-items: center; margin-top: 14px; }
.acl-saved { color: #16a34a; font-size: 12px; }
</style>
