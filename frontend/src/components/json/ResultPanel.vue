<template>
  <div class="result-panel">
    <!-- 顶部工具栏:视图切换(数据/配置 JSON) + 操作 -->
    <div class="panel-header">
      <div class="header-left">
        <component :is="headerIcon" class="header-icon" />
        <span class="title">{{ headerTitle }}</span>
        <a-tag v-if="mode === 'data' && dataMsg?.formattedData?.rowcount !== undefined"
               color="processing" class="field-count">
          {{ dataMsg?.formattedData?.rowcount }} 行
        </a-tag>
        <a-tag v-else-if="mode === 'config' && store.currentConfig" color="processing" class="field-count">
          {{ store.currentConfigFieldCount || 0 }} 字段
        </a-tag>
      </div>
      <div class="actions">
        <!-- 视图切换:同时有数据制品与配置时才可切 -->
        <a-segmented v-if="hasBoth" v-model:value="mode" size="small"
                     :options="[{ value: 'data', label: '数据' }, { value: 'config', label: '配置' }]" />
        <button v-if="mode === 'config'" class="icon-btn" @click="copy" :disabled="!store.currentConfig" title="复制 JSON">
          <CopyOutlined />
        </button>
        <button v-if="mode === 'config'" class="icon-btn" @click="download" :disabled="!store.currentConfig" title="下载 JSON">
          <DownloadOutlined />
        </button>
        <button
          v-if="mode === 'config' && store.isEmbedded"
          class="icon-btn primary"
          @click="applyToParent"
          :disabled="!store.currentConfig"
          title="应用到主系统"
        >
          <CheckOutlined />
        </button>
      </div>
    </div>

    <!-- ══ 数据视图(数据制品: 图表/SQL/表格) ══ -->
    <div v-if="mode === 'data'" class="data-view">
      <!-- 空态:按场景给引导(有配置无数据 vs 两者皆无) -->
      <div v-if="!dataMsg" class="empty">
        <div class="empty-illustration"><BarChartOutlined /></div>
        <p class="empty-title">{{ store.currentConfig ? '还没有数据结果' : '查询结果会显示在这里' }}</p>
        <p class="empty-desc">
          {{ store.currentConfig
            ? '本会话已产出 AI 配置——提问业务问题(如"各城市订单量")即可查询数据, 两种结果可随时切换'
            : '试试问:"各城市的订单数量排名" · "本月销售额趋势" · "各品类销量占比"' }}
        </p>
      </div>

      <template v-else>
        <!-- SQL 段(可折叠) -->
        <div v-if="dataMsg.formattedData?.sql" class="sql-block">
          <div class="sql-head" @click="sqlOpen = !sqlOpen">
            <CodeOutlined class="sql-icon" />
            <span>SQL</span>
            <DownOutlined :class="['sql-arrow', { open: sqlOpen }]" />
          </div>
          <pre v-show="sqlOpen" class="sql-body">{{ dataMsg.formattedData.sql }}</pre>
        </div>

        <!-- 图表/表格(chatbi 复用 BiChartCard 渲染协议) -->
        <div v-if="dataMsg.formattedData?.chart" class="chart-host">
          <BiChartCard :chart="dataMsg.formattedData.chart"
                       :metric-hits="dataMsg.formattedData.metricHits"
                       :artifact="dataMsg.dataResult"
                       :detail="dataMsg.formattedData" />
        </div>

        <!-- 知识库检索结果:子图 + 引用 -->
        <KgGraphCard v-else-if="dataMsg.dataResult?.type === 'kg_search_result'"
                     :result="dataMsg.dataResult"
                     :cited-chunks="citedChunks(dataMsg.content)" />

        <!-- 兜底:键值对展示 -->
        <div v-else class="kv-list">
          <div v-for="(value, key) in displayFields(dataMsg.dataResult)" :key="key" class="kv-row">
            <span class="kv-label">{{ key }}</span>
            <span class="kv-value">{{ value }}</span>
          </div>
        </div>
      </template>
    </div>

    <!-- ══ 配置视图(表单 diff / 完整 JSON) ══ -->
    <div v-else class="editor-container">
      <div v-if="!store.currentConfig" class="empty">
        <div class="empty-illustration"><FileTextOutlined /></div>
        <p class="empty-title">{{ dataMsg ? '本会话还没有 AI 配置' : 'AI 产出的配置会显示在这里' }}</p>
        <p class="empty-desc">
          {{ dataMsg
            ? '已切到配置视图——描述你的表单需求(如"做一个请假申请表, 含姓名/日期/事由")即可生成'
            : '试试说:"帮我做一个请假申请表" · "联系人表加个手机号字段" · "把上一步的字段改成必填"' }}
        </p>
      </div>
      <JsonDiffView v-else :oldObj="store.baselineConfig" :newObj="store.currentConfig" />
    </div>
  </div>
</template>

<script setup lang="ts">
// 右侧结果面板 —— 按制品类型自适应(替代"永远 JSON"):
//   数据制品(chartbi 图表/SQL/表格、知识库子图) → 数据视图;
//   配置制品(表单) → 原 diff 视图(红删绿增)。
// 有数据制品时默认数据视图;两者并存(同会话先问数后改表单)可切换。
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import {
  BarChartOutlined, CheckOutlined, CodeOutlined, CopyOutlined, DownOutlined,
  DownloadOutlined, FileTextOutlined,
} from '@ant-design/icons-vue'
import { useConversationStore } from '../../stores/conversation'
import { copyText } from '../../utils/clipboard'
import BiChartCard from '../chat/BiChartCard.vue'
import KgGraphCard from '../chat/KgGraphCard.vue'
import JsonDiffView from './JsonDiffView.vue'

const emit = defineEmits<{ (e: 'apply'): void }>()

const store = useConversationStore()

const dataMsg = computed(() => store.latestDataMessage)
const hasBoth = computed(() => !!dataMsg.value && !!store.currentConfig)

// 视图模式:有数据制品默认 'data',否则 'config'
const mode = ref<'data' | 'config'>('config')
watch(() => dataMsg.value, (v) => { if (v) mode.value = 'data' }, { immediate: true })
watch(() => store.currentConfig, (v) => { if (v && !dataMsg.value) mode.value = 'config' })

const headerIcon = computed(() => (mode.value === 'data' ? BarChartOutlined : CodeOutlined))
// 标题场景化:数据视图看有没有图;配置视图通用化(平台多 pack——不止表单,
// 标题按当前 pack 的制品类型动态取, 不写死"表单配置")
const headerTitle = computed(() => {
  if (mode.value === 'data') {
    if (!dataMsg.value) return '查询结果'
    if (dataMsg.value.dataResult?.type === 'kg_search_result') return '检索结果'
    return dataMsg.value.formattedData?.chart ? '查询结果 · 图表' : '查询结果 · 明细'
  }
  return store.currentConfig ? 'AI 配置 · 变更对比' : 'AI 配置'
})

const sqlOpen = ref(false)

function citedChunks(text: string): number[] {
  const nums = [...String(text || '').matchAll(/\[片段(\d+)\]/g)].map((m) => parseInt(m[1], 10))
  return [...new Set(nums)]
}

function displayFields(data: Record<string, any> | undefined): Record<string, any> {
  if (!data) return {}
  const out: Record<string, any> = {}
  for (const [k, v] of Object.entries(data)) {
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      out[k] = v
    }
  }
  return out
}

async function copy() {
  if (!store.currentConfig) return
  const ok = await copyText(JSON.stringify(store.currentConfig, null, 2))
  if (ok) message.success('已复制到剪贴板')
  else message.error('复制失败')
}

function download() {
  if (!store.currentConfig) return
  const blob = new Blob([JSON.stringify(store.currentConfig, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${store.currentConfigName || 'config'}-${Date.now()}.json`
  a.click()
  URL.revokeObjectURL(url)
}

function applyToParent() {
  if (!store.currentConfig) return
  emit('apply')
}
</script>

<style scoped>
.result-panel { display: flex; flex-direction: column; height: 100%; background: var(--bg-container); }

.panel-header {
  display: flex; justify-content: space-between; align-items: center;
  padding: 12px 16px; border-bottom: 1px solid var(--border-color-light);
}
.header-left { display: flex; align-items: center; gap: 8px; }
.header-icon { color: var(--color-primary); font-size: 15px; }
.title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.field-count {
  margin-left: 4px !important; border: none !important;
  background: var(--color-primary-light) !important;
  color: var(--color-primary) !important; font-size: 11px;
}
.actions { display: flex; gap: 6px; align-items: center; }
.icon-btn {
  width: 30px; height: 30px; border-radius: var(--radius-md);
  border: 1px solid var(--border-color-light); background: var(--bg-container);
  color: var(--text-regular); cursor: pointer; display: flex;
  align-items: center; justify-content: center; font-size: 13px; transition: all 0.2s;
}
.icon-btn:hover:not(:disabled) {
  border-color: var(--color-primary); color: var(--color-primary);
  background: var(--color-primary-bg);
}
.icon-btn.primary { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }
.icon-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* 数据视图 */
.data-view { flex: 1; overflow: auto; min-height: 0; padding: 14px 16px; display: flex; flex-direction: column; gap: 12px; }
.sql-block { border: 1px solid var(--border-color-light); border-radius: var(--radius-md); overflow: hidden; }
.sql-head {
  display: flex; align-items: center; gap: 6px; padding: 7px 10px;
  background: var(--bg-hover); cursor: pointer; font-size: 12px;
  color: var(--text-regular); user-select: none;
}
.sql-icon { color: var(--color-primary); }
.sql-arrow { margin-left: auto; font-size: 10px; transition: transform 0.2s; }
.sql-arrow.open { transform: rotate(180deg); }
.sql-body {
  margin: 0; padding: 10px; font-family: var(--font-mono); font-size: 11.5px;
  line-height: 1.6; color: var(--text-regular); white-space: pre-wrap;
  word-break: break-all; max-height: 220px; overflow: auto;
}
.chart-host { flex: 1; min-height: 260px; overflow: auto; }
.kv-list { display: flex; flex-direction: column; }
.kv-row {
  display: flex; justify-content: space-between; gap: 12px; padding: 8px 2px;
  border-bottom: 1px solid var(--border-color-lighter); font-size: 13px;
}
.kv-label { color: var(--text-secondary); flex-shrink: 0; }
.kv-value { color: var(--text-primary); word-break: break-all; text-align: right; }

/* 配置视图 */
.editor-container { flex: 1; overflow: auto; min-height: 0; }

.empty {
  height: 100%; display: flex; flex-direction: column; align-items: center;
  justify-content: center; padding: 40px 20px;
}
.empty-illustration {
  width: 64px; height: 64px; border-radius: 50%; background: var(--bg-hover);
  color: var(--text-placeholder); display: flex; align-items: center;
  justify-content: center; font-size: 28px; margin-bottom: 16px;
}
.empty-title { font-size: 14px; color: var(--text-secondary); margin-bottom: 4px; }
.empty-desc { font-size: 12px; color: var(--text-placeholder); }
</style>
