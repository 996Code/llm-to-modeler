<template>
  <div class="bi-chart-card">
    <!-- 命中指标标签行(独立展示, 不参与图表类型链) -->
    <div v-if="metricHits.length" class="metric-hits">
      <a-tag v-for="h in metricHits.slice(0, 5)" :key="h.metric" color="gold" class="metric-tag">
        📊 {{ h.display_name || h.metric }}
      </a-tag>
      <a-tag v-if="metricHits.length > 5" color="default">+{{ metricHits.length - 5 }}</a-tag>
    </div>

    <!-- 对话内明细(独立展示, 不参与图表类型链) -->
    <div v-if="detailLine" class="detail-line">{{ detailLine }}</div>

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
      <div v-if="tableRows.length > 20" class="table-more">
        仅显示前 20 行, 共 {{ tableRows.length }} 行
      </div>
    </div>

    <!-- ECharts 标准图表(pie/bar/line/scatter) -->
    <div v-else ref="chartEl" class="chart-el" :style="{ height: chartHeight }" />
  </div>
</template>

<script setup lang="ts">
// BI 图表卡片 —— chatbi pack 的 formatted.chart 渲染器(ECharts)。
// 机制先例: KgGraphCard(KG 专用图卡)。chart option 由后端 chart_engine 产出,
// 前端只负责渲染 + kpi/table 两种特判视图(与后端约定 1:1)。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'

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
  if (d.chartDegraded) parts.push('图表规则推断')
  return parts.join(' · ')
})

const chartEl = ref<HTMLElement | null>(null)
let instance: echarts.ECharts | null = null

const chartType = computed(() => props.chart?.series?.[0]?.type
  || props.chart?.chart_type || '')
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

const metricHits = computed(() => props.metricHits || [])

function render() {
  if (!chartEl.value || !props.chart) return
  if (!instance) {
    instance = echarts.init(chartEl.value)
  }
  instance.setOption(props.chart, true)
}

onMounted(render)
watch(() => props.chart, () => {
  if (isKpi.value || isTable.value) return
  render()
})
onBeforeUnmount(() => {
  instance?.dispose()
  instance = null
})
</script>

<style scoped>
.bi-chart-card { width: 100%; }
.metric-hits { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
.detail-line { color: #999; font-size: 12px; margin-bottom: 8px; }
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
