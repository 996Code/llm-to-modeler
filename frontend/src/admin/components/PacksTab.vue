<template>
  <div>
    <a-alert type="info" show-icon style="margin: 14px 0 16px" class="pk-alert">
      <template #icon><ThunderboltOutlined /></template>
      <template #message>插件开关热生效,无需重启服务</template>
      <template #description>
        启停状态持久化在 <code>{{ payload?.stateFile ?? 'data/pack_state.json' }}</code>(初始来源:{{ sourceLabel }}),重启后保持。
        已禁用的插件对新会话立即不可见,不会出现在 /api/meta/packs 与宿主插件列表中。
      </template>
    </a-alert>

    <div class="pk-grid">
      <div v-for="pack in payload?.items ?? []" :key="pack.name" class="pk-card" :class="{ off: !pack.enabled }">
        <div class="pk-head">
          <div class="pk-icon" :class="pack.enabled ? 'on' : ''">
            <AppstoreOutlined />
          </div>
          <div class="pk-name-block">
            <div class="pk-name">{{ pack.name }}</div>
            <a-tag v-if="pack.enabled" color="success" class="pk-state">运行中</a-tag>
            <a-tag v-else class="pk-state">已禁用</a-tag>
          </div>
          <a-switch :checked="pack.enabled" :loading="toggling === pack.name" :disabled="!!toggling"
            @click="(checked: boolean | undefined) => toggle(pack, !!checked)" />
        </div>
        <div class="pk-desc">{{ pack.description || '(无描述)' }}</div>
        <div class="pk-foot">
          <span class="pk-meta">
            <a-tooltip v-if="pack.tools.length" :title="pack.tools.join('、')">
              <a-tag color="purple">{{ pack.tools.length }} 个工具</a-tag>
            </a-tooltip>
            <a-tooltip v-else title="插件当前未加载,启用后可见工具清单">
              <a-tag>-</a-tag>
            </a-tooltip>
            <a-tag v-if="pack.artifactType" color="geekblue">{{ pack.artifactType }}</a-tag>
            <a-tag v-for="s in pack.services" :key="s" color="cyan">{{ s }}</a-tag>
          </span>
        </div>
      </div>
    </div>
    <a-empty v-if="payload && !payload.items.length" description="未发现任何插件" style="padding: 60px 0" />
  </div>
</template>

<script setup lang="ts">
// 插件管理:卡片式布局 + 启停开关(热生效)。
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { AppstoreOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import { AdminPack, fetchPacks, setPackEnabled } from '../api'
import type { LoadSafely } from './loadSafely'

const loadSafely = inject<LoadSafely>('loadSafely')!

const payload = ref<Awaited<ReturnType<typeof fetchPacks>> | null>(null)
const toggling = ref('')

const sourceLabel = computed(() => {
  const s = payload.value?.source
  if (s === 'file') return '管理端历史操作'
  if (s === 'env') return 'PACKS_ENABLED 环境变量'
  return '全部发现即启用'
})

async function load() {
  await loadSafely(async () => {
    payload.value = await fetchPacks()
  })
}

async function toggle(pack: AdminPack, enabled: boolean) {
  if (pack.enabled === enabled) return
  toggling.value = pack.name
  await loadSafely(async () => {
    const result = await setPackEnabled(pack.name, enabled)
    payload.value = { items: result.items, stateFile: result.stateFile, source: result.source }
    message.success(
      `${enabled ? '已启用' : '已禁用'}「${pack.name}」,当前生效:${result.loaded?.join('、')}`,
    )
  })
  toggling.value = ''
  await load()  // 无论成败重拉,保证开关与服务端状态一致
}

onMounted(load)
</script>

<style scoped>
.pk-alert :deep(code) {
  background: #f0f5ff; padding: 1px 6px; border-radius: 4px; font-size: 12px; color: #2f54eb;
}
.pk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.pk-card {
  border: 1px solid #e6ebf2; border-radius: 12px; padding: 16px 18px; background: #fff;
  transition: box-shadow 0.15s, opacity 0.15s;
}
.pk-card:hover { box-shadow: 0 6px 18px rgba(30, 41, 82, 0.08); }
.pk-card.off { opacity: 0.72; background: #fbfcfe; }
.pk-head { display: flex; align-items: center; gap: 12px; }
.pk-icon {
  width: 40px; height: 40px; border-radius: 10px; font-size: 18px;
  display: flex; align-items: center; justify-content: center;
  background: #f1f5f9; color: #94a3b8;
}
.pk-icon.on {
  background: linear-gradient(135deg, #eef2ff, #e0e7ff); color: #4f46e5;
}
.pk-name-block { flex: 1; display: flex; align-items: center; gap: 8px; min-width: 0; }
.pk-name { font-weight: 700; font-size: 14px; color: #1f2937; font-family: 'SF Mono', Menlo, Consolas, monospace; }
.pk-state { font-size: 11px; }
.pk-desc {
  margin-top: 10px; font-size: 13px; color: #6b7280; line-height: 1.6;
  min-height: 40px; word-break: break-all;
}
.pk-foot { margin-top: 8px; }
</style>
