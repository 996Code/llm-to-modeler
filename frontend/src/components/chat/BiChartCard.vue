<template>
  <div class="bi-chart-card">
    <!-- 命中指标标签行(独立展示, 不参与图表类型链) -->
    <div v-if="metricHits.length" class="metric-hits">
      <a-tag v-for="h in metricHits.slice(0, 5)" :key="h.metric" color="gold" class="metric-tag">
        📊 {{ h.display_name || h.metric }}
      </a-tag>
      <a-tag v-if="metricHits.length > 5" color="default">+{{ metricHits.length - 5 }}</a-tag>
    </div>

    <!-- 持久化降级提示: 查询成功但记忆/经验未沉淀(非阻断, 结果不受影响) -->
    <div v-if="persistWarningLine" class="persist-warn">
      ⚠ 部分经验未保存: {{ persistWarningLine }}
    </div>

    <!-- KPI 指标卡(链头 v-if, 恢复正确分支) -->
    <div v-if="isKpi" class="kpi-view">
      <div class="kpi-value">{{ kpiValue }}</div>
      <div class="kpi-name">{{ kpiName }}</div>
    </div>

    <!-- 表格视图: 前端渲染明细行 -->
    <div v-else-if="isTable" class="table-view">
      <table>
        <thead><tr><th v-for="c in tableColumns" :key="c">{{ c }}</th></tr></thead>
        <tbody>
          <tr v-for="(row, i) in tableRows.slice(0, 20)" :key="i">
            <td v-for="(cell, j) in row" :key="j">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="totalRows > 20" class="table-more">
        仅显示前 20 行, 共 {{ totalRows }} 行{{ truncated ? ' (已达查询上限, 结果被截断)' : '' }}
      </div>
    </div>

    <!-- ECharts 标准图表(pie/bar/line/scatter) -->
    <div v-else ref="chartEl" class="chart-el" :style="{ height: chartHeight }" />

    <!-- 对话内明细(卡片右下角小字: 耗时/自愈/降级/LLM 用量) -->
    <div v-if="detailLine" class="detail-line">{{ detailLine }}</div>
  </div>
</template>

<script setup lang="ts">
// BI 图表卡片 —— chatbi pack 的 formatted.chart 渲染器(ECharts)。
// 机制先例: KgGraphCard(KG 专用图卡)。chart option 由后端 chart_engine 产出,
// 前端只负责渲染 + kpi/table 两种特判视图(与后端约定 1:1)。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ECharts } from 'echarts/core'

const props = defineProps<{
  chart: Record<string, any> | null | undefined
  metricHits?: Array<{ metric?: string, display_name?: string }>
  artifact?: Record<string, any> | null   // data 制品(table 视图取 columns/rows_sample)
  detail?: Record<string, any> | null     // 对话内明细(totalDurationMs 等)
}>()

const detailLine = computed(() => {
  const d = props.detail || {}
  const parts: string[] = []
  if (d.totalDurationMs !== undefined) parts.push(`总耗时 ${(d.totalDurationMs / 1000).toFixed(1)}s`)
  if (d.executeDurationMs !== undefined) parts.push(`SQL ${(d.executeDurationMs / 1000).toFixed(1)}s`)
  if (d.healRounds > 0) parts.push(`自愈 ${d.healRounds} 轮`)
  if (d.retrievalDegraded) parts.push('检索降级')
  if (d.chartDegraded) parts.push('图表规则推断')
  // LLM 次数与 token(轮末从链路回填;原版 T049 摘要行的等价物, 免开弹窗)
  if (d.llmCallCount) parts.push(`LLM ×${d.llmCallCount}`)
  const tokens = (d.promptTokens || 0) + (d.completionTokens || 0)
  if (tokens > 0) parts.push(`${fmtTokens(tokens)} tokens`)
  return parts.join(' · ')
})

function fmtTokens(n: number): string {
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

// 持久化降级提示(复核报告: 源 persist_warning 等价物)——查询成功但
// 记忆/经验/自动保存没存上时, 用户在结果卡下方直接看到, 不再只留服务端日志
const persistWarningLine = computed(() =>
  (props.detail?.persistWarnings || []).slice(0, 3).join('；'))

const chartEl = ref<HTMLElement | null>(null)
let instance: ECharts | null = null

// chart_type 是业务类型；KPI 的底层 series.type 是 gauge，不能让后者
// 抢先，否则会绕过轻量 KPI 视图并误走 ECharts 通用分支。
const chartType = computed(() => props.chart?.chart_type
  || props.chart?.series?.[0]?.type || '')
const isKpi = computed(() => chartType.value === 'kpi')
const isTable = computed(() => chartType.value === 'table')
const chartHeight = computed(() => (chartType.value === 'pie' ? '260px' : '300px'))

const kpiValue = computed(() => {
  const data = props.chart?.series?.[0]?.data?.[0]
  return data ? Number(data.value).toLocaleString() : '-'
})
const kpiName = computed(() => props.chart?.series?.[0]?.data?.[0]?.name || '')

// table 视图数据: result 页签内由父组件传行;此处从 artifact 提取不了,
// 由 props.chart 特判 + dataResult 行(父级注入)兜底
const tableColumns = computed(() => props.artifact?.columns || [])
const tableRows = computed(() => props.artifact?.rows_sample || [])
// 真实行数: rows_sample 只是样本(≤50), 全量行数在 detail.rowcount
// (此前用样本长度当总数, 1 万行查询显示"共 50 行")
const totalRows = computed(() => props.detail?.rowcount ?? tableRows.value.length)
const truncated = computed(() => Boolean(props.detail?.truncated))

const metricHits = computed(() => props.metricHits || [])

async function render() {
  const target = chartEl.value
  if (!target || !props.chart) return
  const { init } = await import('../../utils/echarts')
  if (chartEl.value !== target || !props.chart) return
  if (!instance) {
    instance = init(target)
  }
  instance.setOption(props.chart, true)
}

// 暴露图表实例(导出 Excel 嵌 PNG 用——原版 ChatView chartInstances 同款)
defineExpose({ getChartInstance: () => instance })

onMounted(render)
watch(() => props.chart, () => {
  if (isKpi.value || isTable.value) {
    instance?.dispose()
    instance = null
    return
  }
  nextTick(render)
})
onBeforeUnmount(() => {
  instance?.dispose()
  instance = null
})
</script>

<style scoped>
.bi-chart-card { width: 100%; }
.metric-hits { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
.detail-line {
  color: #999; font-size: 12px;
  margin-top: 6px; text-align: right;   /* 卡片右下角小字 */
}
.persist-warn { color: #d46b08; font-size: 12px; margin-bottom: 8px; }
.kpi-view { text-align: center; padding: 18px 0; }
.kpi-value { font-size: 40px; font-weight: 700; color: #1677ff; }
.kpi-name { margin-top: 4px; color: #888; font-size: 14px; }
.table-view { overflow-x: auto; }
.table-view table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table-view th, .table-view td { border: 1px solid #eee; padding: 6px 10px; text-align: left; }
.table-view th { background: #fafafa; font-weight: 600; }
.table-more { margin-top: 6px; color: #999; font-size: 12px; }
.chart-el { width: 100%; }
</style>
