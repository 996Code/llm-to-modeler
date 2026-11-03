<template>
  <div v-if="view" class="chatbi-result">
    <div v-if="view.answer" class="result-answer md-content" v-html="renderMarkdown(view.answer)" />
    <BiChartCard
      ref="chartRef"
      :chart="view.chart"
      :artifact="artifact"
      :metric-hits="view.detail.metricHits"
      :detail="view.detail"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import BiChartCard from '../../components/chat/BiChartCard.vue'
import { biPresentation } from './presentation'

const props = defineProps<{
  artifact: Record<string, any>
  summary: string
  labels: Record<string, string>
  renderMarkdown: (text: string) => string
  compact: boolean
  citedChunks: number[]
  detail?: Record<string, any> | null
}>()
const chartRef = ref<InstanceType<typeof BiChartCard> | null>(null)
const view = computed(() => biPresentation({ role: 'assistant', dataResult: props.artifact, content: props.summary }, props.detail))
defineExpose({ getChartInstance: () => chartRef.value?.getChartInstance?.() ?? null })
</script>

<style scoped>
.chatbi-result { width: 100%; min-width: 0; }
.result-answer { font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; margin-bottom: 12px; }
.result-answer :deep(p) { margin: 0 0 8px; }
.result-answer :deep(ul) { padding-left: 20px; margin: 6px 0 12px; }
/* 来源引用徽标(与 ChatPanel 同款 chip;renderMarkdown 注入的 .md-ref
   是纯 DOM,scoped 组件里必须自带一份样式,否则徽标裸奔成普通文本) */
.result-answer :deep(.md-ref) {
  display: inline-block;
  padding: 0 7px;
  margin: 0 2px;
  border: 1px solid currentColor;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 500;
  line-height: 1.7;
  vertical-align: baseline;
  white-space: nowrap;
}
.result-answer :deep(.md-ref-chunk) { background: #f9f0ff; color: #722ed1; }
.result-answer :deep(.md-ref-entity) { background: var(--color-primary-bg); color: var(--color-primary-ink); }
.result-answer :deep(.md-ref-triple) { background: #e6fffb; color: #08979c; }
.result-answer :deep(.md-ref-rel) { background: #fff7e6; color: #d46b08; }
</style>
