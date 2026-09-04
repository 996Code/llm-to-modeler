<template>
  <div>
    <a-alert type="info" show-icon style="margin: 14px 0 16px" class="pk-alert">
      <template #icon><ThunderboltOutlined /></template>
      <template #message>插件开关热生效,无需重启服务</template>
      <template #description>
        启停状态持久化在 <code>{{ payload?.stateFile ?? 'data/pack_state.json' }}</code>(初始来源:{{ sourceLabel }}),重启后保持。
        依赖未配置的插件无法启用——可在插件"设置"里补配连接信息,或配好环境变量后点"重新检测"热加载。
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
            <div class="pk-tags">
              <a-tag v-if="pack.enabled && depOk(pack)" color="success" class="pk-state">运行中</a-tag>
              <a-tag v-else-if="!pack.enabled && depOk(pack)" class="pk-state">已禁用</a-tag>
              <a-tooltip v-else :title="depTooltip(pack)">
                <a-tag :color="pack.dependency?.status === 'probe_failed' ? 'orange' : 'error'" class="pk-state">
                  {{ pack.dependency?.status === 'probe_failed' ? '连接失败' : '依赖未配置' }}
                </a-tag>
              </a-tooltip>
            </div>
          </div>
          <a-switch
            :checked="pack.enabled" :loading="toggling === pack.name"
            :disabled="!!toggling || !depOk(pack)"
            @click="(checked: boolean | undefined) => toggle(pack, !!checked)"
          />
        </div>
        <div class="pk-desc">{{ pack.description || '(无描述)' }}</div>
        <div v-if="!depOk(pack)" class="pk-dep-detail">{{ pack.dependency?.detail }}</div>
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
          <span class="pk-actions">
            <a-tooltip v-if="pack.hasSettings" title="配置连接信息 / 参数(声明式表单)">
              <a @click="openSettings(pack)"><SettingOutlined /> 设置</a>
            </a-tooltip>
            <a-tooltip v-if="!depOk(pack)" title="补配后重新检测依赖,通过则热加载">
              <a :style="{ marginLeft: pack.hasSettings ? '12px' : '0' }" @click="recheck(pack)">
                <SyncOutlined :spin="rechecking === pack.name" /> 重新检测
              </a>
            </a-tooltip>
            <a-tooltip v-if="pack.adminPage && depOk(pack)" :title="`打开「${pack.adminTitle}」管理页`">
              <a :style="{ marginLeft: (pack.hasSettings || !depOk(pack)) ? '12px' : '0' }"
                 @click="$emit('open-page', pack.adminPage)">
                <RightOutlined /> 管理页
              </a>
            </a-tooltip>
          </span>
        </div>
      </div>
    </div>
    <a-empty v-if="payload && !payload.items.length" description="未发现任何插件" style="padding: 60px 0" />

    <!-- 声明式设置抽屉 -->
    <PackSettingsDrawer v-model:open="settingsOpen" :pack-name="settingsPack" @saved="load" />
  </div>
</template>

<script setup lang="ts">
// 插件管理:卡片式布局 + 启停开关(热生效) + 依赖检测徽标 + 声明式设置抽屉。
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  AppstoreOutlined, RightOutlined, SettingOutlined, SyncOutlined, ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { AdminPack, fetchPacks, recheckPack, setPackEnabled } from '../api'
import type { LoadSafely } from './loadSafely'
import PackSettingsDrawer from './PackSettingsDrawer.vue'

const emit = defineEmits<{
  (e: 'open-page', pageKey: string): void
  (e: 'refresh-pages'): void
}>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const payload = ref<Awaited<ReturnType<typeof fetchPacks>> | null>(null)
const toggling = ref('')
const rechecking = ref('')
const settingsOpen = ref(false)
const settingsPack = ref('')

const sourceLabel = computed(() => {
  const s = payload.value?.source
  if (s === 'file') return '管理端历史操作'
  if (s === 'env') return 'PACKS_ENABLED 环境变量'
  return '全部发现即启用'
})

function depOk(pack: AdminPack): boolean {
  return !pack.dependency || pack.dependency.status === 'ok'
}

function depTooltip(pack: AdminPack): string {
  const dep = pack.dependency
  if (!dep) return ''
  const head = dep.status === 'probe_failed' ? '依赖连接失败' : '依赖配置缺失'
  return `${head}:${dep.detail || (dep.missing || []).join('、')}`
}

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
    emit('refresh-pages')  // 动态 Tab 集合可能变化(管理页随插件启停)
  })
  toggling.value = ''
  await load()  // 无论成败重拉,保证开关与服务端状态一致
}

async function recheck(pack: AdminPack) {
  rechecking.value = pack.name
  await loadSafely(async () => {
    const result = await recheckPack(pack.name)
    if (result.dependency.status === 'ok') {
      message.success(
        result.reloaded
          ? `依赖检测通过,「${pack.name}」已热加载`
          : '依赖检测通过' + (pack.enabled ? '' : '(插件处于禁用态,打开开关即可启用)'),
      )
    } else {
      message.warning(`依赖仍不满足:${result.dependency.detail}`)
    }
  })
  rechecking.value = ''
  await load()
  emit('refresh-pages')
}

function openSettings(pack: AdminPack) {
  settingsPack.value = pack.name
  settingsOpen.value = true
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
.pk-name-block { flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.pk-name { font-weight: 700; font-size: 14px; color: #1f2937; font-family: 'SF Mono', Menlo, Consolas, monospace; }
.pk-tags { display: flex; gap: 6px; }
.pk-state { font-size: 11px; }
.pk-desc {
  margin-top: 10px; font-size: 13px; color: #6b7280; line-height: 1.6;
  min-height: 40px; word-break: break-all;
}
.pk-dep-detail {
  margin-top: 4px; font-size: 12px; color: #dc2626; line-height: 1.5; word-break: break-all;
}
.pk-foot { margin-top: 8px; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.pk-meta { display: inline-flex; gap: 4px; flex-wrap: wrap; min-width: 0; }
.pk-actions { white-space: nowrap; font-size: 13px; }
.pk-actions a { color: #2f54eb; }
</style>
