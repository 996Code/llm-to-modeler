<template>
  <div class="integration-page">
    <a-tabs v-model:activeKey="innerTab" class="flat-tabs">
      <a-tab-pane key="packs">
        <template #tab><AppstoreOutlined /> 插件 <span class="pk-tab-count">{{ payload ? `${enabledCount}/${payload.items.length}` : '' }}</span></template>
      </a-tab-pane>
      <a-tab-pane key="mcp">
        <template #tab><ApiOutlined /> MCP 服务 <span v-if="mcpStats.total" class="pk-tab-count">{{ mcpStats.enabled }}/{{ mcpStats.total }}</span></template>
      </a-tab-pane>
      <a-tab-pane key="llm">
        <template #tab><CloudOutlined /> 模型供应商</template>
      </a-tab-pane>
      <template #rightExtra>
        <a-popover :key="innerTab" placement="bottomRight" trigger="click" overlay-class-name="pk-help-pop">
          <template #content>
            <div class="help-pop help-pop-wide" v-html="currentHelp.html"></div>
          </template>
          <a-button type="text" size="small" :aria-label="currentHelp.title" class="help-link">
            <template #icon><QuestionCircleOutlined /></template>说明
          </a-button>
        </a-popover>
      </template>
    </a-tabs>
    <template v-if="innerTab === 'packs'">

    <div class="pk-grid">
      <div v-for="pack in payload?.items ?? []" :key="pack.name" class="pk-card" :class="{ off: !pack.enabled }">
        <div class="pk-head">
          <div class="pk-icon" :class="pack.enabled ? 'on' : ''">
            <AppstoreOutlined />
          </div>
          <div class="pk-name-block">
            <div class="pk-name">{{ pack.adminTitle || pack.name }}</div>
            <div v-if="pack.adminTitle && pack.adminTitle !== pack.name" class="pk-key">{{ pack.name }}</div>
            <div class="pk-tags">
              <a-tag v-if="pack.enabled && depOk(pack) && pack.loaded && !pack.runtimeError" color="success" class="pk-state">运行中</a-tag>
              <a-tooltip v-else-if="pack.enabled && depOk(pack) && (!pack.loaded || !!pack.runtimeError)"
                         :title="pack.runtimeError || '插件未完整装配'">
                <a-tag color="error" class="pk-state">{{ pack.loaded ? '运行异常' : '装配失败' }}</a-tag>
              </a-tooltip>
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
            <a-tooltip v-if="pack.tools.length" :title="pack.tools.map((t: string) => TOOL_LABELS[t] || t).join('、')">
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
            <a-tooltip v-if="!depOk(pack) || (pack.enabled && (!pack.loaded || !!pack.runtimeError))"
                       :title="!depOk(pack) ? '补配后重新检测依赖,通过则热加载' : '重新尝试完整装配/生命周期启动'">
              <a :style="{ marginLeft: pack.hasSettings ? '12px' : '0' }" @click="recheck(pack)">
                <SyncOutlined :spin="rechecking === pack.name" /> 重新检测
              </a>
            </a-tooltip>
            <a-tooltip v-if="pack.adminPage && depOk(pack) && pack.loaded" :title="`打开「${pack.adminTitle}」管理页`">
              <a :style="{ marginLeft: (pack.hasSettings || !depOk(pack) || !pack.loaded) ? '12px' : '0' }"
                 @click="$emit('open-page', pack.adminPage)">
                <RightOutlined /> 管理页
              </a>
            </a-tooltip>
          </span>
        </div>
      </div>
    </div>

    <a-empty v-if="payload && !payload.items.length" description="未发现任何插件" style="padding: 60px 0" />

    </template>
    <McpServersPanel v-if="innerTab === 'mcp'" @count="mcpStats = $event" />
    <LlmProviders v-else-if="innerTab === 'llm'" :embedded="true" />

    <!-- 声明式设置抽屉 -->
    <PackSettingsDrawer v-model:open="settingsOpen" :pack-name="settingsPack" @saved="load" />
  </div>
</template>

<script setup lang="ts">
import { TOOL_LABELS } from '../labels'
// 插件管理:卡片式布局 + 启停开关(热生效) + 依赖检测徽标 + 声明式设置抽屉。
import { computed, inject, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ApiOutlined, AppstoreOutlined, CloudOutlined, QuestionCircleOutlined,
  RightOutlined, SettingOutlined, SyncOutlined,
} from '@ant-design/icons-vue'
import { listMcpServers, AdminPack, fetchPacks, recheckPack, setPackEnabled } from '../api'
import { rememberPacks, packLabel } from '../packDisplay'
import { chip, iconBtn, rules } from '../helpHtml'
import type { LoadSafely } from './loadSafely'
import LlmProviders from './LlmProviders.vue'
import McpServersPanel from './McpServersPanel.vue'
import PackSettingsDrawer from './PackSettingsDrawer.vue'

const emit = defineEmits<{
  (e: 'open-page', pageKey: string): void
  (e: 'refresh-pages'): void
}>()
const loadSafely = inject<LoadSafely>('loadSafely')!

const payload = ref<Awaited<ReturnType<typeof fetchPacks>> | null>(null)
const innerTab = ref('packs')
const mcpStats = ref({ total: 0, enabled: 0 })
onMounted(async () => {
  try {
    const items = await listMcpServers()
    // 面板 @count 已上报过(快速切 tab 场景)则不回退覆盖
    if (mcpStats.value.total === 0) {
      mcpStats.value = { total: items.length,
                         enabled: items.filter((s) => s.enabled !== false).length }
    }
  } catch { /* 计数失败不打扰 */ }
})
const toggling = ref('')
const rechecking = ref('')
const settingsOpen = ref(false)
const settingsPack = ref('')

// 说明与其他页同标准:编号条目 + 彩色状态标签 + 行内按钮图标引用(见 helpHtml.ts)
const helpByTab: Record<string, { title: string; html: string }> = {
  packs: {
    title: '插件说明',
    html: rules([
      '卡片开关启停插件,<b>热切换即时生效</b>,无需重启服务。',
      `状态标签:${chip('green', '运行中')} ${chip('red', '运行异常')} ${chip('red', '装配失败')} ${chip('plain', '已禁用')} ${chip('orange', '连接失败')} ${chip('red', '依赖未配置')}——异常时悬停标签查看原因。`,
      '连接异常时:打开「设置」检查连接信息,再点「重新检测」。',
    ]),
  },
  mcp: {
    title: 'MCP 服务说明',
    html: rules([
      `登记业务平台的服务地址与认证信息,连接成功后可查看工具清单(${iconBtn('reload', '刷新快照(重新探测)')})。`,
      '「服务说明」要写清该系统提供什么业务——它将作为智能客服里<b>能力分组的标题</b>;工具声明不完整的不会进入对话路由。',
      `行内 ${iconBtn('edit', '编辑')} / ${iconBtn('delete', '删除', 'red')};停用服务即停止调用。`,
      '对外接入密钥仅用于其他应用连接本系统,请妥善保管。',
    ]),
  },
  llm: {
    title: '模型供应商说明',
    html: rules([
      '填写主供应商的接口地址、密钥和模型,<b>保存后生效</b>;密钥留空时保留原值。',
      '单价用于费用统计,可以不填;「连通性检测」使用已保存的配置。',
    ]),
  },
}
const currentHelp = computed(() => helpByTab[innerTab.value] || helpByTab.packs)

// 行标题的启用计数(与左侧导航徽标同源语义)
const enabledCount = computed(
  () => payload.value?.items.filter(p => p.enabled).length ?? 0,
)

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
    rememberPacks(payload.value.items)
  })
}

async function toggle(pack: AdminPack, enabled: boolean) {
  if (pack.enabled === enabled) return
  toggling.value = pack.name
  await loadSafely(async () => {
    const result = await setPackEnabled(pack.name, enabled)
    payload.value = { items: result.items, stateFile: result.stateFile, source: result.source }
    message.success(
      `${enabled ? '已启用' : '已禁用'}「${packLabel(pack.name)}」,当前生效:${(result.loaded || []).map(packLabel).join('、')}`,
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
      if (result.reloaded) {
        message.success(`依赖检测通过,「${packLabel(pack.name)}」已热加载`)
      } else if (pack.enabled) {
        message.warning(`依赖检测通过,但「${packLabel(pack.name)}」未完整装配,请查看运行错误`)
      } else {
        message.success('依赖检测通过(插件处于禁用态,打开开关即可启用)')
      }
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
/* 精装层(2026-11 重设计):令牌见 shared/design-tokens.css,规格对齐 design-demos/polished-packs.html。
   卡片圆角/微影/hover 轻抬由 admin.css 全局 .pk-card 提供,此处不再覆盖 hover。 */
.pk-tabs-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.pk-tabs :deep(.ant-tabs-nav) { margin: 0 0 0 -12px; }
.pk-tabs :deep(.ant-tabs-tab) { padding: 6px 12px; font-weight: 500; }
.pk-tab-count {
  margin-left: 6px; font-size: 12px; font-weight: 500; color: var(--brand-ink);
  background: var(--brand-tint); border-radius: 999px; padding: 1px 8px;
}
/* 行标题(左) + 说明入口(右)同一行; 说明不独占一行 */
.pk-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  margin: 10px 2px 2px;
}
.pk-toolbar-title {
  font-size: 14px; font-weight: 600; color: var(--ink);
  display: inline-flex; align-items: center; gap: 8px;
}
.pk-toolbar-count { margin-left: 2px; font-weight: 400; }
.pk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
/* 卡面/圆角/微影/hover 抬升走 admin.css 全局 .pk-card;这里只保留令牌一致的描边底色与过渡 */
.pk-card {
  border: 1px solid var(--line-soft); border-radius: var(--radius-card);
  padding: 16px 18px 14px; background: var(--card);
  transition: box-shadow .18s, transform .18s, border-color .18s, opacity .2s, background .2s;
}
.pk-card.off { opacity: 0.72; background: var(--bg); }
.pk-head { display: flex; align-items: center; gap: 12px; }
.pk-icon {
  width: 40px; height: 40px; border-radius: 10px; font-size: 20px;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  background: var(--bg); color: var(--ink-3);
  transition: background .2s, color .2s;
}
.pk-icon.on { background: var(--brand-tint); color: var(--brand); }
.pk-name-block { flex: 1; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
/* 标题不再用等宽字体;插件标识(key)为常规字重次要色 */
.pk-name { font-size: 15px; font-weight: 650; color: var(--ink); overflow-wrap: anywhere; }
.pk-key { font-size: 12px; font-weight: 400; color: var(--ink-2); line-height: 1.3; }
.pk-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.pk-desc {
  margin-top: 10px; font-size: 14px; color: var(--ink-2); line-height: 1.65;
  min-height: 48px; word-break: break-all;
}
.pk-dep-detail {
  margin-top: 4px; font-size: 12px; color: var(--danger); line-height: 1.5; word-break: break-all;
}
.pk-foot { margin-top: 8px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
.pk-meta { display: inline-flex; gap: 5px; flex-wrap: wrap; min-width: 0; }
.pk-actions { display: inline-flex; align-items: center; white-space: nowrap; font-size: 14px; }
.pk-actions a { color: var(--brand-ink); }
.pk-actions a:hover { text-decoration: underline; text-underline-offset: 3px; }

/* a-tag 胶囊化并把 antd 预设色归一到语义令牌(启用 ok / 停用 ink-3+bg / 异常 danger / 连接失败 warn) */
.pk-state, .pk-meta :deep(.ant-tag) {
  border-radius: 999px; font-size: 12px; line-height: 18px; padding: 0 9px;
}
.pk-state { color: var(--ink-3); background: var(--bg); border-color: var(--bg); }
.pk-state.ant-tag-success { color: var(--ok); background: var(--ok-tint); border-color: var(--ok-tint); }
.pk-state.ant-tag-error { color: var(--danger); background: var(--danger-tint); border-color: var(--danger-tint); }
.pk-state.ant-tag-orange { color: var(--warn); background: var(--warn-tint); border-color: var(--warn-tint); }
.pk-meta :deep(.ant-tag-purple) { color: var(--violet); background: var(--violet-tint); border-color: var(--violet-tint); }
.pk-meta :deep(.ant-tag-geekblue) { color: var(--brand-ink); background: var(--brand-tint); border-color: var(--brand-tint); }
.pk-meta :deep(.ant-tag-cyan) { color: var(--cyan); background: var(--cyan-tint); border-color: var(--cyan-tint); }
.pk-meta :deep(.ant-tag:not(.ant-tag-purple):not(.ant-tag-geekblue):not(.ant-tag-cyan)) {
  color: var(--ink-3); background: var(--bg); border-color: var(--bg);
}
@media (max-width: 700px) { .pk-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
